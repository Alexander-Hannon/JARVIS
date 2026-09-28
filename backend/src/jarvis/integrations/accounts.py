"""Integration accounts, scope grants and provider construction.

Scope is enforced at the application layer in addition to provider OAuth scopes:
the executor only writes to a resource that has an explicit `write` grant on the same account.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from jarvis.config import get_settings
from jarvis.core.db import jdump, jload, row, rows
from jarvis.core.util import audit, new_id, now_iso
from jarvis.security import secrets

# Test/contract overrides: provider name -> adapter instance (e.g. fakes). Labeled simulated.
_overrides: dict[str, Any] = {}


def set_override(provider: str, adapter: Any | None) -> None:
    if adapter is None:
        _overrides.pop(provider, None)
    else:
        _overrides[provider] = adapter


def clear_overrides() -> None:
    _overrides.clear()


def get_account(conn: sqlite3.Connection, provider: str) -> dict | None:
    r = row(conn, "SELECT * FROM integration_accounts WHERE provider=? AND status!='revoked' ORDER BY created_at DESC LIMIT 1",
            (provider,))
    if r:
        r["config"] = jload(r["config"], {})
        r["scopes"] = jload(r["scopes"], [])
    return r


def get_account_by_id(conn: sqlite3.Connection, account_id: str) -> dict | None:
    r = row(conn, "SELECT * FROM integration_accounts WHERE id=?", (account_id,))
    if r:
        r["config"] = jload(r["config"], {})
        r["scopes"] = jload(r["scopes"], [])
    return r


def upsert_account(conn: sqlite3.Connection, provider: str, label: str, secret: str | None, secret_kind: str | None,
                   external_account_id: str | None = None, scopes: list[str] | None = None,
                   config: dict | None = None, status: str = "connected") -> str:
    existing = get_account(conn, provider)
    now = now_iso()
    enc = secrets.encrypt(secret) if secret is not None else None
    if existing and (existing["external_account_id"] == external_account_id or external_account_id is None
                     or existing["external_account_id"] is None):
        cfg = existing["config"] | (config or {})
        conn.execute(
            "UPDATE integration_accounts SET label=?, external_account_id=COALESCE(?, external_account_id), status=?, "
            "secret_ciphertext=COALESCE(?, secret_ciphertext), secret_kind=COALESCE(?, secret_kind), scopes=?, config=?, "
            "last_error=NULL, updated_at=?, version=version+1 WHERE id=?",
            (label, external_account_id, status, enc, secret_kind, jdump(scopes if scopes is not None else existing["scopes"]),
             jdump(cfg), now, existing["id"]))
        aid = existing["id"]
        if existing["external_account_id"] and external_account_id and existing["external_account_id"] != external_account_id:
            pass
    else:
        if existing:
            # A different external account replaces the old one: old grants are dropped so writes
            # can never silently move to another account.
            conn.execute("UPDATE integration_accounts SET status='revoked', updated_at=? WHERE id=?", (now, existing["id"]))
            conn.execute("DELETE FROM scope_grants WHERE account_id=?", (existing["id"],))
        aid = new_id("acct")
        conn.execute(
            "INSERT INTO integration_accounts (id, provider, label, external_account_id, status, secret_ciphertext, secret_kind, "
            "scopes, config, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (aid, provider, label, external_account_id, status, enc, secret_kind, jdump(scopes or []), jdump(config or {}), now, now))
    audit(conn, "user", "integration.connected", f"{provider} connected ({label})", "integration_account", aid)
    return aid


def read_secret(account: dict) -> str | None:
    if not account or account.get("secret_ciphertext") is None:
        return None
    return secrets.decrypt(account["secret_ciphertext"])


def update_secret(conn: sqlite3.Connection, account_id: str, secret: str) -> None:
    conn.execute("UPDATE integration_accounts SET secret_ciphertext=?, updated_at=? WHERE id=?",
                 (secrets.encrypt(secret), now_iso(), account_id))


def set_status(conn: sqlite3.Connection, account_id: str, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE integration_accounts SET status=?, last_error=?, updated_at=? WHERE id=?",
                 (status, error, now_iso(), account_id))


def mark_synced(conn: sqlite3.Connection, account_id: str) -> None:
    conn.execute("UPDATE integration_accounts SET last_sync_at=?, last_error=NULL, status='connected' WHERE id=?",
                 (now_iso(), account_id))


def disconnect(conn: sqlite3.Connection, account_id: str) -> None:
    conn.execute("UPDATE integration_accounts SET status='revoked', secret_ciphertext=NULL, updated_at=? WHERE id=?",
                 (now_iso(), account_id))
    conn.execute("DELETE FROM scope_grants WHERE account_id=?", (account_id,))
    audit(conn, "user", "integration.disconnected", "Integration disconnected; stored secret erased",
          "integration_account", account_id)


# ---------- grants ----------
def grants(conn: sqlite3.Connection, account_id: str, resource_type: str | None = None, access: str | None = None) -> list[dict]:
    sql = "SELECT * FROM scope_grants WHERE account_id=?"
    params: list = [account_id]
    if resource_type:
        sql += " AND resource_type=?"
        params.append(resource_type)
    if access:
        sql += " AND access=?"
        params.append(access)
    return rows(conn, sql, params)


def set_grants(conn: sqlite3.Connection, account_id: str, resource_type: str, access: str,
               resources: list[tuple[str, str | None]], default_target: str | None = None) -> None:
    conn.execute("DELETE FROM scope_grants WHERE account_id=? AND resource_type=? AND access=?",
                 (account_id, resource_type, access))
    for rid, name in resources:
        conn.execute("INSERT INTO scope_grants (id, account_id, resource_type, resource_id, resource_name, access, "
                     "is_default_target, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (new_id("grant"), account_id, resource_type, rid, name, access, int(rid == default_target), now_iso()))
    audit(conn, "user", "integration.scope_changed", f"{resource_type} {access} grants set ({len(resources)})",
          "integration_account", account_id, {"resources": [r[0] for r in resources]})


def has_grant(conn: sqlite3.Connection, account_id: str, resource_type: str, resource_id: str, access: str) -> bool:
    return row(conn, "SELECT 1 FROM scope_grants WHERE account_id=? AND resource_type=? AND resource_id=? AND access=?",
               (account_id, resource_type, resource_id, access)) is not None


def default_target(conn: sqlite3.Connection, account_id: str, resource_type: str) -> dict | None:
    return row(conn, "SELECT * FROM scope_grants WHERE account_id=? AND resource_type=? AND access='write' "
                     "AND is_default_target=1", (account_id, resource_type))


# ---------- adapters ----------
class NotConnected(Exception):
    def __init__(self, provider: str, detail: str = ""):
        super().__init__(f"{provider} is not connected. {detail}".strip())
        self.provider = provider


def task_provider(conn: sqlite3.Connection):
    if "todoist" in _overrides:
        return _overrides["todoist"]
    acct = get_account(conn, "todoist")
    if not acct or acct["status"] not in ("connected", "error"):
        raise NotConnected("todoist", "Connect it in Settings → Integrations.")
    from jarvis.integrations.todoist import TodoistAdapter
    return TodoistAdapter(read_secret(acct))


def _google_credentials(conn: sqlite3.Connection, acct: dict):
    from jarvis.integrations.google_oauth import GoogleCredentials
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret):
        raise NotConnected("google", "Google OAuth client is not configured (JARVIS_GOOGLE_CLIENT_ID/SECRET).")
    tokens = json.loads(read_secret(acct) or "{}")
    aid = acct["id"]

    def updater(new_tokens: dict) -> None:
        from jarvis.core.db import connection
        with connection() as c2:
            update_secret(c2, aid, json.dumps(new_tokens))
    return GoogleCredentials(tokens=tokens, client_id=s.google_client_id, client_secret=s.google_client_secret,
                             token_updater=updater)


def calendar_provider(conn: sqlite3.Connection):
    if "google_calendar" in _overrides:
        return _overrides["google_calendar"]
    acct = get_account(conn, "google")
    if not acct or acct["status"] not in ("connected", "error") or "calendar" not in acct["config"].get("purposes", []):
        raise NotConnected("google_calendar", "Connect Google Calendar in Settings → Integrations.")
    from jarvis.integrations.google_calendar import GoogleCalendarAdapter
    return GoogleCalendarAdapter(_google_credentials(conn, acct))


def drive_provider(conn: sqlite3.Connection):
    if "google_drive" in _overrides:
        return _overrides["google_drive"]
    acct = get_account(conn, "google")
    if not acct or "drive" not in acct["config"].get("purposes", []):
        raise NotConnected("google_drive", "Connect Google Drive in Settings → Integrations.")
    from jarvis.integrations.google_drive import GoogleDriveAdapter
    return GoogleDriveAdapter(_google_credentials(conn, acct))


def github_provider(conn: sqlite3.Connection):
    if "github" in _overrides:
        return _overrides["github"]
    acct = get_account(conn, "github")
    if not acct:
        raise NotConnected("github")
    from jarvis.integrations.github import GitHubAdapter
    return GitHubAdapter(read_secret(acct))


def is_simulated(adapter: Any) -> bool:
    return bool(getattr(adapter, "simulated", False))
