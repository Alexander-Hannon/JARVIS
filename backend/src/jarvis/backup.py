"""Backup / restore with checksums and schema compatibility.

Backups contain the SQLite DB (consistent online backup) + stored document files, NOT the
encryption key (kept separately; without it encrypted tokens in a restored DB are unreadable — by design)
and NOT model weights / audio. Restore:
  1. verify checksum + schema version <= current code,
  2. take a pre-restore backup of the current state,
  3. replace the DB, then neutralize anything that could cause fresh writes: queued/leased jobs
     cancelled, unrevoked approvals revoked, proposals awaiting approval expired, schedules disabled
     (AJ re-enables them), actions paused. Completed history, memories, sources and settings survive.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from jarvis.config import get_settings
from jarvis.core.db import _connect, migrate, reset_init_cache, schema_version
from jarvis.core.util import new_id, now_iso


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(label: str = "manual") -> dict:
    s = get_settings()
    s.backups_dir.mkdir(parents=True, exist_ok=True)
    bid = new_id("bkp")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = _connect(s.db_path)
        dst = sqlite3.connect(str(tdp / "jarvis.db"))
        src.backup(dst)
        ver = schema_version(src)
        dst.close()
        src.close()
        manifest = {"id": bid, "created_at": now_iso(), "label": label, "schema_version": ver,
                    "db_sha256": _sha(tdp / "jarvis.db"), "includes": ["database", "document files"],
                    "excludes": ["encryption key (secret.key)", "model weights", "audio", ".env"]}
        (tdp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        out = s.backups_dir / f"{bid}.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(tdp / "manifest.json", arcname="manifest.json")
            tar.add(tdp / "jarvis.db", arcname="jarvis.db")
            if s.files_dir.exists():
                tar.add(s.files_dir, arcname="files")
    sha = _sha(out)
    (s.backups_dir / f"{bid}.sha256").write_text(sha)
    return {"id": bid, "path": str(out), "sha256": sha, "bytes": out.stat().st_size, "schema_version": ver, "label": label}


def list_backups() -> list[dict]:
    s = get_settings()
    out = []
    for p in sorted(s.backups_dir.glob("bkp_*.tar.gz"), reverse=True):
        side = p.with_suffix("").with_suffix(".sha256")
        out.append({"id": p.name.split(".")[0], "path": str(p), "bytes": p.stat().st_size,
                    "sha256": side.read_text().strip() if side.exists() else None})
    return out


def _open(bid: str) -> tuple[Path, dict]:
    s = get_settings()
    p = s.backups_dir / f"{bid}.tar.gz"
    if not p.exists() or "/" in bid:
        raise FileNotFoundError(bid)
    side = s.backups_dir / f"{bid}.sha256"
    if side.exists() and side.read_text().strip() != _sha(p):
        raise ValueError("Backup checksum mismatch — file corrupted or altered.")
    with tarfile.open(p) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
    return p, manifest


def restore_preview(bid: str) -> dict:
    p, m = _open(bid)
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(p) as tar:
            tar.extract("jarvis.db", td, filter="data")
        c = sqlite3.connect(str(Path(td) / "jarvis.db"))
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # nosec B608 table names from a literal tuple
                  for t in ("memories", "source_documents", "proposals", "briefings", "task_links", "calendar_links")}
        pending = {
            "queued_jobs_to_cancel": c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','leased','paused')").fetchone()[0],
            "open_approvals_to_revoke": c.execute("SELECT COUNT(*) FROM approvals WHERE revoked_at IS NULL").fetchone()[0],
            "schedules_to_disable": c.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0],
        }
        db_ok = _sha(Path(td) / "jarvis.db") == m["db_sha256"]
        c.close()
    from jarvis.core.db import migration_files
    current = max(v for v, _, _ in migration_files())
    return {"backup": m, "compatible": m["schema_version"] <= current and db_ok, "db_checksum_ok": db_ok, "counts": counts,
            "will_disable": pending, "pre_restore_backup": True,
            "note": "Encrypted integration tokens only work if the same secret.key is present."}


def restore(bid: str) -> dict:
    prev = restore_preview(bid)
    if not prev["compatible"]:
        raise ValueError("Backup is not compatible with this version (or failed checksum).")
    pre = create_backup(label=f"pre-restore of {bid}")
    s = get_settings()
    p, _ = _open(bid)
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(p) as tar:
            tar.extractall(td, filter="data")
        tdp = Path(td)
        # Online backup API copies pages into the live DB safely even while other connections are open.
        src = sqlite3.connect(str(tdp / "jarvis.db"))
        c = _connect(s.db_path)
        src.backup(c)
        src.close()
        if (tdp / "files").exists():
            shutil.copytree(tdp / "files", s.files_dir, dirs_exist_ok=True)
    reset_init_cache()
    migrate(c)
    now = now_iso()
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE status IN ('queued','leased','paused')", (now,))
    c.execute("UPDATE approvals SET revoked_at=? WHERE revoked_at IS NULL", (now,))
    c.execute("UPDATE proposed_actions SET state='cancelled', last_error='Cancelled by restore', updated_at=? "
              "WHERE state IN ('proposed','queued','approved','paused')", (now,))
    c.execute("UPDATE proposed_actions SET last_error='Outcome unknown before restore; reconcile via Retry' WHERE state IN "
              "('executing','unknown')")
    c.execute("UPDATE proposed_actions SET state='unknown' WHERE state='executing'")
    c.execute("UPDATE proposals SET status='expired', updated_at=? WHERE status IN ('proposed','queued','approved')", (now,))
    c.execute("UPDATE schedules SET enabled=0, next_run_at=NULL, updated_at=?", (now,))
    c.execute("UPDATE control_state SET actions_paused=1, paused_at=?, paused_reason='Restored from backup — review, then resume'",
              (now,))
    c.execute("INSERT INTO audit_events (at, actor, kind, summary, data) VALUES (?,?,?,?,?)",
              (now, "user", "backup.restored", f"Restored {bid}; jobs/approvals neutralized, schedules disabled, actions paused",
               json.dumps({"pre_restore_backup": pre["id"]})))
    c.execute("COMMIT")
    c.close()
    return {"ok": True, "pre_restore_backup_id": pre["id"], "restored": bid}
