"""Readiness diagnostics (`jarvis doctor` and /api/doctor). Reports facts; never makes paid or write calls."""
from __future__ import annotations

import os
import shutil
import sqlite3

from jarvis.config import get_settings
from jarvis.core.db import migration_files, row, rows, schema_version
from jarvis.integrations import accounts as A
from jarvis.security import secrets


def _c(name: str, status: str, detail: str, remedy: str | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, "remedy": remedy}


def checks(conn: sqlite3.Connection) -> list[dict]:
    s = get_settings()
    out = []
    ver = schema_version(conn)
    latest = max(v for v, _, _ in migration_files())
    out.append(_c("Database", "ok" if ver == latest else "fail", f"SQLite schema v{ver} at {s.db_path}",
                  None if ver == latest else "Restart Jarvis to apply migrations."))
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    out.append(_c("Foreign keys", "ok" if fk else "fail", "enforced" if fk else "disabled"))
    ks = secrets.key_status()
    out.append(_c("Token encryption key", "ok" if ks["ok"] else "fail",
                  f"source={ks.get('source')} {ks.get('mode', '')} (only secret columns are encrypted; the DB file itself is not — "
                  f"rely on OS disk encryption)", None if ks["ok"] else "Run `jarvis setup-key` or set JARVIS_SECRET_KEY."))
    owner = row(conn, "SELECT 1 FROM owner WHERE id=1")
    out.append(_c("Owner account", "ok" if owner else "warn", "configured" if owner else "not set up",
                  None if owner else "Open the app on this machine and complete first-run setup."))
    out.append(_c("Network exposure", "ok" if s.host in ("127.0.0.1", "localhost") else "warn",
                  f"bound to {s.host}:{s.port}" + (" (loopback only)" if s.host in ("127.0.0.1", "localhost") else ""),
                  None if s.host in ("127.0.0.1", "localhost") else "Only expose through authenticated HTTPS (see DEPLOYMENT.md)."))
    # voice
    from jarvis.voice.stt import get_engine, hardware_profile
    st = get_engine().status()
    hw = hardware_profile()
    lat = row(conn, "SELECT ms, at FROM latency_metrics WHERE kind='transcription' ORDER BY id DESC LIMIT 1")
    out.append(_c("Speech-to-text (local)", "ok" if st["downloaded"] else "warn",
                  f"{st['engine']} {st['model']} on {st['device']}/{st['compute_type']}; downloaded={st['downloaded']} "
                  f"(~{st['download_size_mb']} MB); state={st['state']}; CPU cores={hw['cpu_count']} CUDA={hw['cuda']}"
                  + (f"; last measured transcription {lat['ms']} ms" if lat else "; no transcription measured yet"),
                  None if st["downloaded"] else "Settings → Voice → Warm up (downloads the model), or `jarvis stt-warmup`."))
    out.append(_c("Microphone", "pending", "Browser-side: requires a secure context (localhost or HTTPS) and permission.",
                  "Test from the Command console; iPhone Safari needs HTTPS."))
    out.append(_c("Speech output (TTS)", "pending", "Browser speechSynthesis; voice list depends on the OS/browser.",
                  "Pick a voice in Settings → Voice."))
    # model provider
    from jarvis.llm.provider import get_provider
    ok, why = get_provider().available(conn)
    out.append(_c("Model provider (Anthropic)", "ok" if ok else "warn", why if not ok else f"configured; model {s.model_default}",
                  None if ok else "Add an API key and choose a budget; offline capture/tasks still work."))
    # integrations
    for prov, label in (("todoist", "Todoist"), ("google", "Google (Calendar/Drive)"), ("ics", "Deadline feed (ICS)"),
                        ("github", "GitHub (read-only)")):
        a = A.get_account(conn, prov)
        if not a:
            out.append(_c(label, "warn" if prov in ("todoist", "google") else "pending", "not connected",
                          "Settings → Integrations"))
            continue
        g = A.grants(conn, a["id"])
        live = row(conn, "SELECT 1 FROM verification_receipts r JOIN proposed_actions p ON p.id=r.action_id WHERE p.account_id=? "
                         "AND r.ok=1 AND r.simulated=0 LIMIT 1", (a["id"],))
        status = "ok" if a["status"] == "connected" else "fail"
        out.append(_c(label, status, f"{a['status']} as {a['label']}; {len(g)} scope grant(s); last sync {a['last_sync_at']}; "
                                     f"live write verified: {'yes' if live else 'not yet'}"
                      + (f"; error: {a['last_error']}" if a["last_error"] else ""),
                      "Reconnect in Settings" if status == "fail" else None))
    if s.google_client_id and s.google_client_secret:
        from jarvis.integrations.service import redirect_uri
        out.append(_c("Google OAuth client", "ok", f"configured; redirect URI {redirect_uri()}"))
    else:
        out.append(_c("Google OAuth client", "warn", "JARVIS_GOOGLE_CLIENT_ID/SECRET not set", "See Settings → Integrations → Google setup."))
    # scheduler
    from jarvis.worker import status as wstatus
    w = wstatus(conn)
    enabled = rows(conn, "SELECT label, next_run_at, last_success_at FROM schedules WHERE enabled=1")
    out.append(_c("Worker / scheduler", "ok" if w["alive"] else "warn",
                  f"heartbeat {w['last_beat_at']}; {len(enabled)} schedule(s) enabled" + (f"; {w['note']}" if w.get("note") else ""),
                  None if w["alive"] else "Run `jarvis worker` (keep host awake; see DEPLOYMENT.md for always-on)."))
    from jarvis.briefings.notify import push_config
    pc = push_config()
    subs = row(conn, "SELECT COUNT(*) c FROM push_subscriptions WHERE revoked_at IS NULL")["c"]
    out.append(_c("Notifications", "ok" if pc["supported"] and subs else "warn",
                  f"in-app inbox ready; web push {'configured' if pc['supported'] else 'not configured'}; {subs} subscription(s)",
                  pc.get("reason")))
    free = shutil.disk_usage(s.data_dir).free // (1024 ** 3)
    out.append(_c("Disk space", "ok" if free > 2 else "warn", f"{free} GB free in data dir"))
    paused = row(conn, "SELECT actions_paused FROM control_state WHERE id=1")
    out.append(_c("Action control", "warn" if paused and paused["actions_paused"] else "ok",
                  "Actions PAUSED" if paused and paused["actions_paused"] else "actions enabled (approval still required)"))
    out.append(_c("Telemetry", "ok", "none; no third-party tracking"))
    env_perm = None
    envp = s.data_dir.parent.parent / ".env"
    if envp.exists():
        env_perm = oct(os.stat(envp).st_mode & 0o777)
    out.append(_c(".env permissions", "ok" if env_perm in (None, "0o600", "0o400") else "warn", f"{env_perm or 'no .env'}",
                  "chmod 600 .env" if env_perm not in (None, "0o600", "0o400") else None))
    return out


def main() -> None:
    from jarvis.core.db import connection
    with connection() as conn:
        for c in checks(conn):
            mark = {"ok": "✔", "warn": "!", "fail": "✘", "pending": "…"}[c["status"]]
            print(f"{mark} {c['name']}: {c['detail']}")
            if c["remedy"]:
                print(f"    → {c['remedy']}")
