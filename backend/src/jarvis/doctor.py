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


def _security_checks(conn: sqlite3.Connection, s, owner: bool) -> list[dict]:
    """V5 M0 baseline (docs/security/NIST_800-171_MAPPING.md). Facts only; enterprise installs tighten the defaults."""
    out = []
    idle_ok = s.session_idle_hours <= 24 * 7
    out.append(_c("Session limits", "ok" if idle_ok else "warn",
                  f"idle {s.session_idle_hours:g} h, absolute {s.session_ttl_hours} h",
                  None if idle_ok else "Lower JARVIS_SESSION_IDLE_HOURS (enterprise policy is usually 0.25)."))
    if s.remote_mode:
        if not s.trusted_proxies:
            out.append(_c("Client IP attribution", "warn", "remote mode without JARVIS_TRUSTED_PROXIES: every request is "
                          "attributed (and throttled) as the proxy's address",
                          'Set JARVIS_TRUSTED_PROXIES=["127.0.0.1"] when behind `tailscale serve` or a local reverse proxy.'))
        else:
            out.append(_c("Client IP attribution", "ok", f"X-Forwarded-For trusted from {', '.join(s.trusted_proxies)}"))
        https = (s.public_origin or "").startswith("https://")
        out.append(_c("HTTPS / HSTS", "ok" if https else "fail",
                      "HSTS and Secure cookies on" if https else "remote mode without an https:// public origin",
                      None if https else "Set JARVIS_PUBLIC_ORIGIN to the https:// address of your HTTPS front."))
    from datetime import timedelta

    from jarvis.core.util import iso, utcnow
    # ISO strings compare correctly only against ISO strings (not SQLite's datetime() format).
    from jarvis.security import mfa
    pol = mfa.policy()
    owing = [u["username"] for u in rows(conn, "SELECT id, username, role FROM users WHERE status='active'")
             if mfa.required(u["role"]) and not mfa.enrolled(conn, u["id"])]
    out.append(_c("Second factor (MFA)", "fail" if pol == "off" else ("warn" if owing else "ok"),
                  f"policy {pol}" + (f"; not enrolled yet: {', '.join(owing[:5])}" if owing else ""),
                  "Set JARVIS_MFA_POLICY to privileged or all." if pol == "off" else
                  ("They'll be asked to enroll at their next sign-in." if owing else None)))
    fails = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE ok=0 AND at>=?",
                         (iso(utcnow() - timedelta(days=1)),)).fetchone()[0]
    out.append(_c("Failed sign-ins (24 h)", "ok" if fails < 20 else "warn", f"{fails} failed login/pairing/setup attempts",
                  None if fails < 20 else "Review Activity → auth events; someone may be guessing."))
    if not owner:
        out.append(_c("First-run setup", "warn", "no owner yet; setup from another device needs the console token",
                      "Run `jarvis setup-token` on this machine (or read it from the `jarvis serve` console)."))
    return out


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
    owner = row(conn, "SELECT 1 FROM users WHERE role='owner' AND status='active' LIMIT 1")
    n_users = conn.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    out.append(_c("Owner account", "ok" if owner else "warn", f"configured ({n_users} active user(s))" if owner else "not set up",
                  None if owner else "Open the app on this machine and complete first-run setup."))
    out.append(_c("Network exposure", "ok" if s.host in ("127.0.0.1", "localhost") else "warn",
                  f"bound to {s.host}:{s.port}" + (" (loopback only)" if s.host in ("127.0.0.1", "localhost") else ""),
                  None if s.host in ("127.0.0.1", "localhost") else "Only expose through authenticated HTTPS (see DEPLOYMENT.md)."))
    out.extend(_security_checks(conn, s, bool(owner)))
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
    # Doctor is an admin view of the whole install: per provider, how many people connected it and whether any are
    # failing. With a single connection (a personal install) it shows that account's details as before.
    for prov, label in (("todoist", "Todoist"), ("google", "Google (Calendar/Drive)"), ("ics", "Deadline feed (ICS)"),
                        ("github", "GitHub (read-only)")):
        accts = rows(conn, "SELECT id FROM integration_accounts WHERE provider=? AND status!='revoked'", (prov,))
        if len(accts) > 1:
            bad = conn.execute("SELECT COUNT(*) FROM integration_accounts WHERE provider=? AND status NOT IN "
                               "('connected','revoked')", (prov,)).fetchone()[0]
            out.append(_c(label, "warn" if bad else "ok", f"{len(accts)} people connected; {bad} need attention",
                          "Ask them to reconnect in Settings → Integrations" if bad else None))
            continue
        a = A.get_account_by_id(conn, accts[0]["id"]) if accts else None
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
