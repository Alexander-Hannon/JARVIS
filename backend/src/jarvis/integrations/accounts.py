"""Integration accounts, scope grants and provider construction.

Accounts are personal: each row belongs to one user, and every lookup is by (user, provider). Connecting a provider
replaces only *your own* previous account for it; an external account already connected by someone else is refused
rather than taken over. Org-level accounts (the Anthropic key, the Telegram bot) have no user and are managed by admins.

Scope is enforced at the application layer in addition to provider OAuth scopes:
the executor only writes to a resource that has an explicit `write` grant on the same account, and only for the
account's own user.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from jarvis.config import get_settings
from jarvis.core.db import jdump, jload, row, rows
from jarvis.core.util import audit, new_id, now_iso
from jarvis.security import secrets

ORG_PROVIDERS = frozenset({"anthropic", "telegram"})
OWNER = "usr_owner"

# Test/contract overrides: (user, provider) -> adapter instance (e.g. fakes). Labeled simulated. They apply to one
# user (the first owner unless given), so multi-user tests prove another user does NOT get the fake.
_overrides: dict[tuple[str, str], Any] = {}


def _okey(uid: str | None, provider: str) -> tuple[str, str]:
    return ("" if provider in ORG_PROVIDERS else (uid or ""), provider)


def set_override(provider: str, adapter: Any | None, uid: str = OWNER) -> None:
    if adapter is None:
        _overrides.pop(_okey(uid, provider), None)
    else:
        _overrides[_okey(uid, provider)] = adapter


def clear_overrides() -> None:
    _overrides.clear()


def _override(uid: str | None, provider: str) -> Any | None:
    return _overrides.get(_okey(uid, provider))


class AccountInUse(Exception):
    """The external account is already connected by another user of this install."""


def _decode(r: dict | None) -> dict | None:
    if r:
        r["config"] = jload(r["config"], {})
        r["scopes"] = jload(r["scopes"], [])
    return r


def get_account(conn: sqlite3.Connection, uid: str, provider: str) -> dict | None:
    if provider in ORG_PROVIDERS:
        return org_account(conn, provider)
    return _decode(row(conn, "SELECT * FROM integration_accounts WHERE user_id=? AND provider=? AND status!='revoked' "
                             "ORDER BY created_at DESC LIMIT 1", (uid, provider)))


def org_account(conn: sqlite3.Connection, provider: str) -> dict | None:
    return _decode(row(conn, "SELECT * FROM integration_accounts WHERE user_id IS NULL AND provider=? "
                             "AND status!='revoked' ORDER BY created_at DESC LIMIT 1", (provider,)))


def get_account_by_id(conn: sqlite3.Connection, account_id: str, uid: str | None = None) -> dict | None:
    """With `uid`, only that user's account (None for anyone else's — callers answer 404)."""
    if uid is None:
        return _decode(row(conn, "SELECT * FROM integration_accounts WHERE id=?", (account_id,)))
    return _decode(row(conn, "SELECT * FROM integration_accounts WHERE id=? AND user_id=?", (account_id, uid)))


def list_accounts(conn: sqlite3.Connection, uid: str) -> list[dict]:
    """The user's own accounts plus org-level ones (shown read-only to non-admins)."""
    return [_decode(r) for r in rows(conn, "SELECT * FROM integration_accounts WHERE user_id=? OR user_id IS NULL "
                                           "ORDER BY created_at", (uid,))]


def upsert_account(conn: sqlite3.Connection, uid: str | None, provider: str, label: str, secret: str | None,
                   secret_kind: str | None, external_account_id: str | None = None, scopes: list[str] | None = None,
                   config: dict | None = None, status: str = "connected") -> str:
    if (uid is None) != (provider in ORG_PROVIDERS):
        raise ValueError(f"{provider}: {'org' if provider in ORG_PROVIDERS else 'personal'} account expected")
    existing = org_account(conn, provider) if uid is None else get_account(conn, uid, provider)
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
    else:
        if existing:
            # A different external account replaces *this user's* old one: old grants are dropped so writes can never
            # silently move to another account.
            conn.execute("UPDATE integration_accounts SET status='revoked', updated_at=? WHERE id=?", (now, existing["id"]))
            conn.execute("DELETE FROM scope_grants WHERE account_id=?", (existing["id"],))
        taken = row(conn, "SELECT id, user_id, status FROM integration_accounts WHERE provider=? AND external_account_id=?",
                    (provider, external_account_id)) if external_account_id else None
        if taken and taken["status"] != "revoked" and taken["user_id"] != uid:
            raise AccountInUse(f"That {provider} account is already connected by another user.")
        if taken:
            # Same external account reconnecting (or a revoked row being reused): start clean for the new owner.
            conn.execute("DELETE FROM scope_grants WHERE account_id=?", (taken["id"],))
            conn.execute("UPDATE integration_accounts SET user_id=?, label=?, status=?, secret_ciphertext=?, secret_kind=?, "
                         "scopes=?, config=?, last_error=NULL, updated_at=?, version=version+1 WHERE id=?",
                         (uid, label, status, enc, secret_kind, jdump(scopes or []), jdump(config or {}), now, taken["id"]))
            aid = taken["id"]
        else:
            aid = new_id("acct")
            conn.execute(
                "INSERT INTO integration_accounts (id, user_id, provider, label, external_account_id, status, "
                "secret_ciphertext, secret_kind, scopes, config, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, uid, provider, label, external_account_id, status, enc, secret_kind, jdump(scopes or []),
                 jdump(config or {}), now, now))
    audit(conn, "user", "integration.connected", f"{provider} connected ({label})", "integration_account", aid, uid=uid)
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


def disconnect(conn: sqlite3.Connection, account_id: str, by: str | None = None) -> None:
    """The caller has already checked the account belongs to `by` (or `by` is an admin for org accounts)."""
    conn.execute("UPDATE integration_accounts SET status='revoked', secret_ciphertext=NULL, updated_at=? WHERE id=?",
                 (now_iso(), account_id))
    conn.execute("DELETE FROM scope_grants WHERE account_id=?", (account_id,))
    audit(conn, "user", "integration.disconnected", "Integration disconnected; stored secret erased",
          "integration_account", account_id, uid=by)


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
    owner = row(conn, "SELECT user_id FROM integration_accounts WHERE id=?", (account_id,))
    if not owner or owner["user_id"] is None:
        raise ValueError("grants belong to a personal account")
    conn.execute("DELETE FROM scope_grants WHERE account_id=? AND resource_type=? AND access=?",
                 (account_id, resource_type, access))
    for rid, name in resources:
        conn.execute("INSERT INTO scope_grants (id, account_id, resource_type, resource_id, resource_name, access, "
                     "is_default_target, created_at, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                     (new_id("grant"), account_id, resource_type, rid, name, access, int(rid == default_target), now_iso(),
                      owner["user_id"]))
    audit(conn, "user", "integration.scope_changed", f"{resource_type} {access} grants set ({len(resources)})",
          "integration_account", account_id, {"resources": [r[0] for r in resources]}, uid=owner["user_id"])


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


def task_provider(conn: sqlite3.Connection, uid: str):
    if (o := _override(uid, "todoist")) is not None:
        return o
    acct = get_account(conn, uid, "todoist")
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


def calendar_provider(conn: sqlite3.Connection, uid: str):
    if (o := _override(uid, "google_calendar")) is not None:
        return o
    acct = get_account(conn, uid, "google")
    if not acct or acct["status"] not in ("connected", "error") or "calendar" not in acct["config"].get("purposes", []):
        raise NotConnected("google_calendar", "Connect Google Calendar in Settings → Integrations.")
    from jarvis.integrations.google_calendar import GoogleCalendarAdapter
    return GoogleCalendarAdapter(_google_credentials(conn, acct))


def drive_provider(conn: sqlite3.Connection, uid: str):
    if (o := _override(uid, "google_drive")) is not None:
        return o
    acct = get_account(conn, uid, "google")
    if not acct or "drive" not in acct["config"].get("purposes", []):
        raise NotConnected("google_drive", "Connect Google Drive in Settings → Integrations.")
    from jarvis.integrations.google_drive import GoogleDriveAdapter
    return GoogleDriveAdapter(_google_credentials(conn, acct))


def github_provider(conn: sqlite3.Connection, uid: str):
    if (o := _override(uid, "github")) is not None:
        return o
    acct = get_account(conn, uid, "github")
    if not acct:
        raise NotConnected("github")
    from jarvis.integrations.github import GitHubAdapter
    return GitHubAdapter(read_secret(acct))


def is_simulated(adapter: Any) -> bool:
    return bool(getattr(adapter, "simulated", False))


# ---------- mail (Gmail rides on the Google account with purpose "gmail"; Outlook on the Microsoft account) ----------
MAILBOXES = ("gmail", "outlook")
# Gmail has its own account row (it can be a different Google account than Calendar); Outlook rides on Microsoft.
_MAIL_ACCOUNT = {"gmail": ("gmail", "mail"), "outlook": ("microsoft", "mail")}


def mail_account(conn: sqlite3.Connection, uid: str, box: str) -> dict | None:
    provider, purpose = _MAIL_ACCOUNT[box]
    acct = get_account(conn, uid, provider)
    if acct and purpose in acct["config"].get("purposes", []):
        return acct
    return None


def connected_mailboxes(conn: sqlite3.Connection, uid: str) -> list[str]:
    return [b for b in MAILBOXES if _override(uid, b) is not None or mail_account(conn, uid, b)]


def _microsoft_credentials(conn: sqlite3.Connection, acct: dict):
    from jarvis.integrations.microsoft import MicrosoftCredentials
    s = get_settings()
    if not (s.microsoft_client_id and s.microsoft_client_secret):
        raise NotConnected("outlook", "Microsoft OAuth client is not configured (JARVIS_MICROSOFT_CLIENT_ID/SECRET).")
    tokens = json.loads(read_secret(acct) or "{}")
    aid = acct["id"]

    def updater(new_tokens: dict) -> None:
        from jarvis.core.db import connection
        with connection() as c2:
            update_secret(c2, aid, json.dumps(new_tokens))
    return MicrosoftCredentials(tokens, s.microsoft_client_id, s.microsoft_client_secret, s.microsoft_tenant,
                                token_updater=updater)


def mail_provider(conn: sqlite3.Connection, uid: str, box: str):
    if (o := _override(uid, box)) is not None:
        return o
    acct = mail_account(conn, uid, box)
    if not acct or acct["status"] not in ("connected", "error"):
        raise NotConnected(box, "Connect it in Settings → Integrations.")
    if box == "gmail":
        from jarvis.integrations.gmail import GmailAdapter
        return GmailAdapter(_google_credentials(conn, acct))
    from jarvis.integrations.microsoft import OutlookAdapter
    return OutlookAdapter(_microsoft_credentials(conn, acct))


# ---------- V4 app connectors (Notion, Slack, Canvas, Spotify, Home Assistant, routing, SimpleFIN) ----------
APP_PROVIDERS = ("notion", "slack", "canvas", "spotify", "homeassistant", "routing", "simplefin")


def app_provider(conn: sqlite3.Connection, uid: str, provider: str):
    if (o := _override(uid, provider)) is not None:
        return o
    acct = get_account(conn, uid, provider)
    if not acct or acct["status"] not in ("connected", "error"):
        raise NotConnected(provider, "Connect it in Settings → Integrations.")
    secret, cfg = read_secret(acct), acct["config"]
    if provider == "notion":
        from jarvis.integrations.notion import NotionAdapter
        return NotionAdapter(secret)
    if provider == "slack":
        from jarvis.integrations.slack import SlackAdapter
        return SlackAdapter(secret)
    if provider == "canvas":
        from jarvis.integrations.canvas import CanvasAdapter
        return CanvasAdapter(cfg["base_url"], secret)
    if provider == "homeassistant":
        from jarvis.integrations.homeassistant import HomeAssistantAdapter
        return HomeAssistantAdapter(cfg["base_url"], secret)
    if provider == "routing":
        from jarvis.integrations.routing import RoutingAdapter
        return RoutingAdapter(secret)
    if provider == "simplefin":
        from jarvis.integrations.simplefin import SimpleFinAdapter
        return SimpleFinAdapter(secret)
    if provider == "spotify":
        from jarvis.integrations.spotify import SpotifyAdapter, SpotifyCredentials
        s = get_settings()
        aid = acct["id"]

        def updater(new_tokens: dict) -> None:
            from jarvis.core.db import connection
            with connection() as c2:
                update_secret(c2, aid, json.dumps(new_tokens))
        return SpotifyAdapter(SpotifyCredentials(json.loads(secret or "{}"), s.spotify_client_id or "",
                                                 token_updater=updater))
    raise NotConnected(provider, "Unknown provider.")


def app_connected(conn: sqlite3.Connection, uid: str, provider: str) -> bool:
    if _override(uid, provider) is not None:
        return True
    acct = get_account(conn, uid, provider)
    return bool(acct and acct["status"] in ("connected", "error"))
