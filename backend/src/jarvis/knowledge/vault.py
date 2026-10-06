"""Index a workspace folder (Obsidian vault / Markdown notes) into searchable evidence.

A user opts one of their workspace roots into indexing (Settings → Local files); the notes become *their* documents. The worker re-indexes indexed roots
periodically. Reuses the normal ingest pipeline: identity is `origin='vault'`, `origin_ref='<root_id>:<relative path>'`,
and unchanged files dedupe by content hash. Notes deleted from disk are forgotten from search. The same path rules as
the local operator apply: no hidden folders (so `.obsidian/`, `.trash/` are skipped), no secret-looking files, no
symlinks out of the root. Note text is untrusted data like any document.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from jarvis.core import settings as S
from jarvis.core.db import rows, tx
from jarvis.core.util import audit, now_iso
from jarvis.knowledge.ingest import delete_document, ingest_bytes
from jarvis.localops import files as F

INDEX_EXT = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
SKIP_DIRS = {"node_modules", "__pycache__"}
MAX_FILES = 3000
MAX_FILE_BYTES = 5 * 1024 * 1024


def indexed_roots(conn, uid: str) -> list[str]:
    known = {r["id"] for r in F.list_roots(conn, uid)}
    return [r for r in (S.user_data(conn, uid).get("vault") or {}).get("indexed_roots", []) if r in known]


def all_indexed(conn) -> list[tuple[str, str]]:
    """(user_id, root_id) for every user's indexed roots — the worker's re-index list."""
    out = []
    for u in rows(conn, "SELECT user_id FROM user_settings"):
        out += [(u["user_id"], r) for r in indexed_roots(conn, u["user_id"])]
    return out


def set_indexed(conn: sqlite3.Connection, uid: str, root_id: str, enabled: bool) -> list[str]:
    F.get_root(conn, uid, root_id)   # raises for unknown (or someone else's) roots
    out: list[str] = []
    with tx(conn):
        def upd(data: dict) -> None:
            cur = set((data.get("vault") or {}).get("indexed_roots", []))
            cur = cur | {root_id} if enabled else cur - {root_id}
            data.setdefault("vault", {})["indexed_roots"] = sorted(cur)
            out.extend(sorted(cur))
        S.update_user_data(conn, uid, upd)
        audit(conn, "user", "vault.index_" + ("enabled" if enabled else "disabled"), f"Search indexing "
              f"{'on' if enabled else 'off'} for workspace root", "scope_grant", root_id, uid=uid)
    if not enabled:
        forget_root(conn, uid, root_id)
    return out


def _walk(base: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        for f in filenames:
            if f.startswith(".") or F._is_secretish(f) or Path(f).suffix.lower() not in INDEX_EXT:
                continue
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield p


def index_root(conn: sqlite3.Connection, uid: str, root_id: str) -> dict:
    root = F.get_root(conn, uid, root_id)
    base = Path(root["path"]).resolve()
    stats = {"new_or_changed": 0, "unchanged": 0, "removed": 0, "skipped": 0, "failed": 0}
    seen: set[str] = set()
    for p in _walk(base):
        rel = p.relative_to(base).as_posix()
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                stats["skipped"] += 1
                continue
            F.resolve_in_root(root, rel)          # same containment/hidden/secret checks as the operator
            data = p.read_bytes()
        except (OSError, F.FileOpError):
            stats["skipped"] += 1
            continue
        ref = f"{root_id}:{rel}"
        seen.add(ref)
        res = ingest_bytes(conn, uid, data, p.name, INDEX_EXT[p.suffix.lower()], None, origin="vault", origin_ref=ref)
        if res.get("status") == "failed":
            stats["failed"] += 1
        stats["unchanged" if res.get("deduped") else "new_or_changed"] += 1
    for d in rows(conn, "SELECT id, origin_ref FROM source_documents WHERE user_id=? AND origin='vault' "
                        "AND origin_ref LIKE ? AND deleted_at IS NULL", (uid, f"{root_id}:%")):
        if d["origin_ref"] not in seen:
            delete_document(conn, uid, d["id"])
            stats["removed"] += 1
    with tx(conn):
        S.update_user_data(conn, uid, lambda data: data.setdefault("vault", {}).setdefault("last_index", {}).__setitem__(
            root_id, {"at": now_iso(), **stats}))
    return stats


def forget_root(conn: sqlite3.Connection, uid: str, root_id: str) -> int:
    n = 0
    for d in rows(conn, "SELECT id FROM source_documents WHERE user_id=? AND origin='vault' AND origin_ref LIKE ? "
                        "AND deleted_at IS NULL", (uid, f"{root_id}:%")):
        delete_document(conn, uid, d["id"])
        n += 1
    return n


def status(conn, uid: str) -> dict:
    v = S.user_data(conn, uid).get("vault") or {}
    return {"indexed_roots": indexed_roots(conn, uid), "last_index": v.get("last_index", {})}
