"""Connection flows and sync jobs for integrations (policy + persistence around the raw adapters).

Every flow is for one user (`uid`): connecting, choosing calendars/projects, syncing. OAuth callbacks arrive without
the session cookie, so the user is taken from the server-side OAuth state row created by their own session."""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
from datetime import timedelta

from jarvis.config import get_settings
from jarvis.core import settings as S
from jarvis.core.db import row, rows, tx
from jarvis.core.util import audit, iso, now_iso, parse_iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError
from jarvis.security.secrets import seal, unseal


class SetupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _upsert(conn, uid: str | None, provider: str, *a, **kw) -> str:
    try:
        return A.upsert_account(conn, uid, provider, *a, **kw)
    except A.AccountInUse as e:
        raise SetupError("account_in_use", str(e)) from None


# ------------------------------------------------------------------ Todoist
def connect_todoist(conn, uid: str, token: str) -> dict:
    from jarvis.integrations.todoist import TodoistAdapter
    token = token.strip()
    if len(token) < 20:
        raise SetupError("bad_token", "That doesn't look like a Todoist API token.")
    try:
        me = TodoistAdapter(token).whoami()
    except ProviderError as e:
        raise SetupError(e.kind, f"Todoist rejected the token ({e.kind}).") from e
    aid = _upsert(conn, uid, "todoist", me.get("email") or me.get("full_name") or "Todoist", token, "api_token",
                           external_account_id=str(me.get("id")), scopes=["data:read_write (personal token)"])
    return integration_view(conn, aid)


def todoist_projects(conn, uid: str) -> list[dict]:
    tp = A.task_provider(conn, uid)
    return [{"id": str(p["id"]), "name": p.get("name")} for p in tp.list_projects()]


def set_todoist_target(conn, uid: str, project_id: str, area_projects: dict | None = None) -> dict:
    acct = A.get_account(conn, uid, "todoist")
    if not acct:
        raise SetupError("not_connected", "Connect Todoist first.")
    projects = {p["id"]: p["name"] for p in todoist_projects(conn, uid)}
    if project_id not in projects:
        raise SetupError("unknown_project", "That project isn't in your Todoist account.")
    chosen = {project_id} | set((area_projects or {}).values())
    unknown = [p for p in chosen if p not in projects]
    if unknown:
        raise SetupError("unknown_project", f"Unknown Todoist project(s): {unknown}")
    with tx(conn):
        A.set_grants(conn, acct["id"], "todoist_project", "write", [(p, projects[p]) for p in chosen], default_target=project_id)
        if area_projects is not None:
            S.update_user_data(conn, uid, lambda d: d.setdefault("todoist", {}).__setitem__("area_projects", area_projects))
    from jarvis.agents.specialists import refresh_open_destinations
    refresh_open_destinations(conn, uid)
    return integration_view(conn, acct["id"])


def todoist_sync(conn, uid: str) -> dict:
    """Pull one user's remote state and reconcile their Jarvis-created tasks (completed/deleted/edited externally)."""
    tp = A.task_provider(conn, uid)
    remote = {t.id: t for t in tp.list_tasks()}
    changed = 0
    for tl in rows(conn, "SELECT * FROM task_links WHERE user_id=? AND status IN ('active','pending_sync','unknown') "
                         "AND external_id IS NOT NULL", (uid,)):
        t = remote.get(tl["external_id"]) or tp.get_task(tl["external_id"])
        new_status = "deleted" if t is None or t.is_deleted else "completed" if t.is_completed else "active"
        if new_status != tl["status"] or (t and t.content != tl["content"]):
            conn.execute("UPDATE task_links SET status=?, content=COALESCE(?, content), last_synced_at=?, updated_at=? WHERE id=?",
                         (new_status, t.content if t else None, now_iso(), now_iso(), tl["id"]))
            changed += 1
    acct = A.get_account(conn, uid, "todoist")
    if acct and not A.is_simulated(tp):
        A.mark_synced(conn, acct["id"])
    from jarvis.agents import context
    context.invalidate()
    return {"pulled": len(remote), "reconciled": changed}


# ------------------------------------------------------------------ Google OAuth
GOOGLE_PURPOSES = ("calendar", "drive", "gmail")
GMAIL_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.compose"]


def google_setup_info() -> dict:
    from jarvis.integrations.google_oauth import CALENDAR_SCOPES, DRIVE_FILE_SCOPES, DRIVE_SCOPES
    s = get_settings()
    return {"configured": bool(s.google_client_id and s.google_client_secret), "redirect_uri": redirect_uri(),
            "redirect_uris": redirect_uris(),
            "required_apis": ["Google Calendar API", "Google Drive API (only if using Drive)", "Gmail API (only if using Gmail)"],
            "scopes": {"calendar": CALENDAR_SCOPES, "drive": DRIVE_SCOPES, "gmail": GMAIL_SCOPES,
                       "drive_per_file_alternative": DRIVE_FILE_SCOPES},
            "steps": [
                "Google Cloud Console → create a project (personal account; no Workspace admin / domain-wide delegation).",
                "APIs & Services → enable Google Calendar API (and Drive API if wanted).",
                "OAuth consent screen → External, Testing mode, add your own Google account as a test user; add the scopes listed.",
                "Credentials → Create OAuth client ID → Web application → add EACH of these Authorized redirect URIs: "
                + ", ".join(redirect_uris()),
                "Put JARVIS_GOOGLE_CLIENT_ID and JARVIS_GOOGLE_CLIENT_SECRET in .env (never commit), restart Jarvis, then Connect.",
            ],
            "drive_tradeoff": "drive.readonly lets Jarvis read files you select by ID/folder (Jarvis itself restricts reads to "
                              "the roots you allow); drive.file only covers files opened/created with this app and is NOT "
                              "arbitrary read access to existing files."}


def redirect_uri(origin: str | None = None) -> str:
    """OAuth callback on the address AJ is actually using (localhost or the Tailscale origin), so the provider sends the
    browser back where the binding cookie lives. Only JARVIS's own allowed origins are honored; anything else falls back
    to the configured default. Each address must be registered as a redirect URI with the provider."""
    s = get_settings()
    default = s.public_origin or f"http://{'localhost' if s.host in ('127.0.0.1', 'localhost') else s.host}:{s.port}"
    base = default
    if origin:
        from jarvis.api.app import allowed_origins
        o = origin.rstrip("/")
        if o in allowed_origins() and not o.endswith(":5173"):     # the Vite dev server can't receive callbacks
            base = o
    return base.rstrip("/") + "/api/integrations/google/callback"


def redirect_uris() -> list[str]:
    """Every callback address AJ should register (one per way he opens JARVIS)."""
    s = get_settings()
    out = [f"http://localhost:{s.port}/api/integrations/google/callback"]
    if s.public_origin:
        out.append(s.public_origin.rstrip("/") + "/api/integrations/google/callback")
    return out


def google_start(conn, uid: str, purpose: str, origin: str | None = None) -> tuple[str, str]:
    """Returns (auth_url, browser_binding). The caller sets the binding as a short-lived Lax cookie."""
    from jarvis.integrations.google_oauth import CALENDAR_SCOPES, DRIVE_SCOPES, build_auth_url, generate_pkce_pair
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret):
        raise SetupError("not_configured", "Google OAuth client isn't configured; follow the setup steps first.")
    if purpose not in GOOGLE_PURPOSES:
        raise SetupError("bad_purpose", f"purpose must be one of {', '.join(GOOGLE_PURPOSES)}")
    state = pysecrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    binding = pysecrets.token_urlsafe(32)
    ru = redirect_uri(origin)
    conn.execute("INSERT INTO oauth_states (state_hash, provider, purpose, code_verifier, redirect_uri, created_at, expires_at, "
                 "binding_hash, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                 (hashlib.sha256(state.encode()).hexdigest(), "google", purpose, seal(verifier), ru, now_iso(),
                  iso(utcnow() + timedelta(minutes=10)), hashlib.sha256(binding.encode()).hexdigest(), uid))
    scopes = {"calendar": CALENDAR_SCOPES, "drive": DRIVE_SCOPES, "gmail": GMAIL_SCOPES}[purpose]
    return build_auth_url(s.google_client_id, ru, scopes, state, challenge), binding


def google_callback(conn, state: str, code: str | None, error: str | None, binding: str | None = None) -> dict:
    from jarvis.integrations.google_oauth import exchange_code, fetch_userinfo
    h = hashlib.sha256((state or "").encode()).hexdigest()
    st = row(conn, "SELECT * FROM oauth_states WHERE state_hash=?", (h,))
    if not st or st["used_at"] or parse_iso(st["expires_at"]) < utcnow() or st["provider"] != "google":
        raise SetupError("bad_state", "OAuth state is invalid, reused or expired. Start the connection again.")
    import hmac
    if not binding or not st["binding_hash"] or not hmac.compare_digest(
            hashlib.sha256(binding.encode()).hexdigest(), st["binding_hash"]):
        raise SetupError("wrong_browser", "Finish the connection in the same browser where you clicked Connect.")
    conn.execute("UPDATE oauth_states SET used_at=? WHERE state_hash=?", (now_iso(), h))
    if error or not code:
        raise SetupError("denied", f"Google returned: {error or 'no code'}")
    s = get_settings()
    try:
        tokens = exchange_code(s.google_client_id, s.google_client_secret, st["redirect_uri"], code, unseal(st["code_verifier"]))
        info = fetch_userinfo(tokens["access_token"])
    except ProviderError as e:
        raise SetupError("token_exchange_failed", f"Google token exchange failed ({e.kind}).") from e
    uid = st["user_id"]          # the user whose session started this connection
    if st["purpose"] == "gmail":
        # Gmail is its own account slot: it may be a different Google account (personal Gmail) than Calendar/Drive
        # (school account), and connecting it must never replace or drop the Calendar account's grants.
        prev = A.get_account(conn, uid, "gmail")
        if prev and prev["external_account_id"] == info.get("email") and not tokens.get("refresh_token"):
            tokens["refresh_token"] = json.loads(A.read_secret(prev) or "{}").get("refresh_token")
        aid = _upsert(conn, uid, "gmail", info.get("email") or "Gmail", json.dumps(tokens), "oauth_tokens",
                               external_account_id=info.get("email"), scopes=(tokens.get("scope") or "").split(),
                               config={"purposes": ["mail"]})
        return integration_view(conn, aid)
    existing = A.get_account(conn, uid, "google")
    purposes = set(existing["config"].get("purposes", [])) if existing and existing["external_account_id"] == info.get("email") else set()
    purposes.add(st["purpose"])
    if existing and existing["external_account_id"] == info.get("email"):
        old = json.loads(A.read_secret(existing) or "{}")
        if not tokens.get("refresh_token") and old.get("refresh_token"):
            tokens["refresh_token"] = old["refresh_token"]
    granted = (tokens.get("scope") or "").split()
    aid = _upsert(conn, uid, "google", info.get("email") or "Google account", json.dumps(tokens), "oauth_tokens",
                           external_account_id=info.get("email"), scopes=granted, config={"purposes": sorted(purposes)})
    return integration_view(conn, aid)


def google_calendars(conn, uid: str) -> list[dict]:
    cp = A.calendar_provider(conn, uid)
    acct = A.get_account(conn, uid, "google")
    reads = {g["resource_id"] for g in A.grants(conn, acct["id"], "calendar", "read")} if acct else set()
    tgt = A.default_target(conn, acct["id"], "calendar") if acct else None
    return [{"id": c["id"], "summary": c.get("summary"), "accessRole": c.get("accessRole"), "primary": c.get("primary", False),
             "timeZone": c.get("timeZone"), "selected_read": c["id"] in reads,
             "write_target": bool(tgt and tgt["resource_id"] == c["id"])} for c in cp.list_calendars()]


def set_calendars(conn, uid: str, read_ids: list[str], write_target: str | None) -> dict:
    acct = A.get_account(conn, uid, "google")
    if not acct:
        raise SetupError("not_connected", "Connect Google Calendar first.")
    cals = {c["id"]: c for c in A.calendar_provider(conn, uid).list_calendars()}
    bad = [c for c in read_ids if c not in cals]
    if bad:
        raise SetupError("unknown_calendar", f"Unknown calendar(s): {bad}")
    with tx(conn):
        A.set_grants(conn, acct["id"], "calendar", "read", [(c, cals[c].get("summary")) for c in read_ids])
        if write_target:
            c = cals.get(write_target)
            if not c:
                raise SetupError("unknown_calendar", "Unknown write calendar.")
            if c.get("accessRole") != "owner":
                raise SetupError("not_owner", "Jarvis only writes to calendars you own (not shared/other people's).")
            A.set_grants(conn, acct["id"], "calendar", "write", [(write_target, c.get("summary"))], default_target=write_target)
        else:
            A.set_grants(conn, acct["id"], "calendar", "write", [])
    from jarvis.agents import context
    from jarvis.agents.specialists import refresh_open_destinations
    context.invalidate()
    refresh_open_destinations(conn, uid)
    return integration_view(conn, acct["id"])


# ------------------------------------------------------------------ Drive (selected roots, read-only)
def set_drive_roots(conn, uid: str, ids: list[str]) -> dict:
    acct = A.get_account(conn, uid, "google")
    if not acct or "drive" not in acct["config"].get("purposes", []):
        raise SetupError("not_connected", "Connect Google Drive first.")
    dp = A.drive_provider(conn, uid)
    items = []
    for i in ids:
        meta = dp.get_file(i)
        items.append((i, meta.get("name"), meta.get("mimeType")))
    with tx(conn):
        A.set_grants(conn, acct["id"], "drive_folder", "read", [(i, n) for i, n, m in items if m == "application/vnd.google-apps.folder"])
        A.set_grants(conn, acct["id"], "drive_file", "read", [(i, n) for i, n, m in items if m != "application/vnd.google-apps.folder"])
    return integration_view(conn, acct["id"])


def drive_allowed(conn, uid: str, file_meta: dict) -> bool:
    acct = A.get_account(conn, uid, "google")
    if not acct:
        return False
    files = {g["resource_id"] for g in A.grants(conn, acct["id"], "drive_file", "read")}
    folders = {g["resource_id"] for g in A.grants(conn, acct["id"], "drive_folder", "read")}
    return file_meta["id"] in files or bool(set(file_meta.get("parents") or []) & folders)


def drive_ingest(conn, uid: str, file_id: str) -> dict:
    """Fetch (or refresh if changed) a selected Drive file into the user's evidence store."""
    from jarvis.knowledge.ingest import ingest_bytes
    dp = A.drive_provider(conn, uid)
    meta = dp.get_file(file_id)
    if not drive_allowed(conn, uid, meta):
        raise SetupError("not_selected", "That Drive file isn't inside a folder/file you selected for Jarvis.")
    data, mime = dp.download(file_id, meta["mimeType"], 20 * 1024 * 1024)
    name = meta.get("name", file_id)
    if mime == "text/plain" and not name.endswith(".txt"):
        name += ".txt"
    elif mime == "application/pdf" and not name.endswith(".pdf"):
        name += ".pdf"
    elif mime == "text/csv" and not name.endswith(".csv"):
        name += ".csv"
    res = ingest_bytes(conn, uid, data, name, mime, None, origin="drive", origin_ref=file_id)
    return res | {"remote_modified": meta.get("modifiedTime"), "link": meta.get("webViewLink")}


# ------------------------------------------------------------------ ICS deadline feed
def connect_ics(conn, uid: str, url: str) -> dict:
    from jarvis.security.netguard import UnsafeURL, validate_url
    try:
        clean = validate_url(url.strip())
    except UnsafeURL as e:
        raise SetupError("unsafe_url", str(e)) from e
    # The external id is per user: each person's feed URL is their own secret.
    aid = _upsert(conn, uid, "ics", "Deadline feed", clean, "ics_url", external_account_id=f"ics-feed:{uid}",
                  scopes=["read"])
    return integration_view(conn, aid)


def ics_refresh(conn, uid: str, fetcher=None) -> dict:
    from jarvis.agents import context
    from jarvis.knowledge import ics
    from jarvis.security.netguard import UnsafeURL, safe_get
    acct = A.get_account(conn, uid, "ics")
    if not acct:
        raise SetupError("not_connected", "No deadline feed configured.")
    url = A.read_secret(acct)
    try:
        body = fetcher(url) if fetcher else safe_get(url).content
    except (UnsafeURL, Exception) as e:  # never log the URL
        A.set_status(conn, acct["id"], "error", f"Feed refresh failed: {type(e).__name__}")
        # `from None`: chained httpx errors embed the private feed URL; never let it reach logs/tracebacks.
        raise SetupError("fetch_failed", f"Feed refresh failed ({type(e).__name__}); last good data kept.") from None
    today = context.local_today(conn, uid)
    ws, we = today - timedelta(days=7), today + timedelta(days=90)
    try:
        items = ics.parse_feed(body, ws, we, context.user_tz(conn, uid))
    except Exception as e:
        A.set_status(conn, acct["id"], "error", f"Feed could not be parsed: {type(e).__name__}")
        raise SetupError("parse_failed", "Feed could not be parsed; last good data kept.") from e
    keys: set[str] = set()
    try:
        tz = context.user_tz(conn, uid)
        cal = context.events_between(conn, uid, context.day_bounds(ws, tz)[0], context.day_bounds(we, tz)[1])
        for e in cal["events"]:
            d = e["start"][:10] if e["all_day"] else parse_iso(e["start"]).astimezone(__import__("zoneinfo").ZoneInfo(tz)).date().isoformat()
            keys.add(ics.calendar_key(e["summary"] or "", d))
    except Exception:
        pass
    with tx(conn):
        counts = ics.upsert_deadlines(conn, acct["id"], items, frozenset(keys), window_start=ws, window_end=we)
        A.mark_synced(conn, acct["id"])
        audit(conn, "worker", "ics.refreshed", f"Deadline feed refreshed: {counts}", "integration_account", acct["id"],
              uid=uid)
    return counts | {"items": len(items), "last_refresh": now_iso()}


# ------------------------------------------------------------------ GitHub (read-only)
def connect_github(conn, uid: str, token: str, repos: list[str]) -> dict:
    from jarvis.integrations.github import GitHubAdapter
    gh = GitHubAdapter(token.strip())
    for r in repos:
        try:
            gh.repo_status(r)
        except ProviderError as e:
            raise SetupError(e.kind, f"Can't read {r}: {e.message}") from e
    aid = _upsert(conn, uid, "github", "GitHub (read-only)", token.strip(), "fine_grained_pat",
                  external_account_id=f"github:{uid}", scopes=["metadata:read", "issues:read", "pull_requests:read"])
    A.set_grants(conn, aid, "github_repo", "read", [(r, r) for r in repos])
    return integration_view(conn, aid)


def github_status(conn, uid: str) -> list[dict]:
    acct = A.get_account(conn, uid, "github")
    if not acct:
        return []
    gh = A.github_provider(conn, uid)
    out = []
    for g in A.grants(conn, acct["id"], "github_repo", "read"):
        try:
            out.append(gh.repo_status(g["resource_id"]))
        except ProviderError as e:
            out.append({"repo": g["resource_id"], "error": e.kind})
    return out


def connect_anthropic(conn, key: str) -> dict:
    key = key.strip()
    if not key.startswith("sk-ant-"):
        raise SetupError("bad_key", "That doesn't look like an Anthropic API key.")
    aid = _upsert(conn, None, "anthropic", "Anthropic API", key, "api_key", external_account_id="anthropic")   # org-level
    return integration_view(conn, aid) | {"note": "Key stored encrypted. No test call is made until you enable a budget."}


# ------------------------------------------------------------------ Microsoft (Outlook mail)
def microsoft_redirect_uri(origin: str | None = None) -> str:
    return redirect_uri(origin).replace("/google/callback", "/microsoft/callback")


def microsoft_setup_info() -> dict:
    from jarvis.integrations.microsoft import MAIL_SCOPES, SEND_SCOPE
    s = get_settings()
    return {"configured": bool(s.microsoft_client_id and s.microsoft_client_secret), "redirect_uri": microsoft_redirect_uri(),
            "tenant": s.microsoft_tenant, "scopes": MAIL_SCOPES, "send_scope_added_when_enabled": SEND_SCOPE,
            "steps": [
                "Azure portal → Microsoft Entra ID → App registrations → New registration.",
                "Supported account types: 'Accounts in any organizational directory and personal Microsoft accounts'.",
                "Redirect URI: platform Web, exactly: " + microsoft_redirect_uri(),
                "Certificates & secrets → New client secret; copy the value.",
                "API permissions → Microsoft Graph → Delegated: User.Read, Mail.ReadWrite, offline_access (Mail.Send only "
                "if you'll enable sending).",
                "Put JARVIS_MICROSOFT_CLIENT_ID and JARVIS_MICROSOFT_CLIENT_SECRET in .env (never commit), restart, Connect.",
            ]}


def microsoft_start(conn, uid: str, purpose: str = "mail", with_send: bool = False,
                    origin: str | None = None) -> tuple[str, str]:
    from jarvis.integrations.google_oauth import generate_pkce_pair
    from jarvis.integrations.microsoft import MAIL_SCOPES, SEND_SCOPE, build_auth_url
    s = get_settings()
    if not (s.microsoft_client_id and s.microsoft_client_secret):
        raise SetupError("not_configured", "Microsoft OAuth client isn't configured; follow the setup steps first.")
    if purpose != "mail":
        raise SetupError("bad_purpose", "purpose must be mail")
    state = pysecrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    binding = pysecrets.token_urlsafe(32)
    ru = microsoft_redirect_uri(origin)
    conn.execute("INSERT INTO oauth_states (state_hash, provider, purpose, code_verifier, redirect_uri, created_at, expires_at, "
                 "binding_hash, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                 (hashlib.sha256(state.encode()).hexdigest(), "microsoft", purpose + ("+send" if with_send else ""), seal(verifier),
                  ru, now_iso(), iso(utcnow() + timedelta(minutes=10)), hashlib.sha256(binding.encode()).hexdigest(), uid))
    scopes = MAIL_SCOPES + ([SEND_SCOPE] if with_send else [])
    return build_auth_url(s.microsoft_tenant, s.microsoft_client_id, ru, scopes, state, challenge), binding


def microsoft_callback(conn, state: str, code: str | None, error: str | None, binding: str | None) -> dict:
    import hmac

    from jarvis.integrations.microsoft import MAIL_SCOPES, SEND_SCOPE, exchange_code, fetch_me
    h = hashlib.sha256((state or "").encode()).hexdigest()
    st = row(conn, "SELECT * FROM oauth_states WHERE state_hash=?", (h,))
    if not st or st["used_at"] or parse_iso(st["expires_at"]) < utcnow() or st["provider"] != "microsoft":
        raise SetupError("bad_state", "OAuth state is invalid, reused or expired. Start the connection again.")
    if not binding or not st["binding_hash"] or not hmac.compare_digest(
            hashlib.sha256(binding.encode()).hexdigest(), st["binding_hash"]):
        raise SetupError("wrong_browser", "Finish the connection in the same browser where you clicked Connect.")
    conn.execute("UPDATE oauth_states SET used_at=? WHERE state_hash=?", (now_iso(), h))
    if error or not code:
        raise SetupError("denied", f"Microsoft returned: {error or 'no code'}")
    s = get_settings()
    scopes = MAIL_SCOPES + ([SEND_SCOPE] if st["purpose"].endswith("+send") else [])
    try:
        tokens = exchange_code(s.microsoft_tenant, s.microsoft_client_id, s.microsoft_client_secret, st["redirect_uri"],
                               code, unseal(st["code_verifier"]), scopes)
        me = fetch_me(tokens["access_token"])
    except ProviderError as e:
        raise SetupError("token_exchange_failed", f"Microsoft token exchange failed ({e.kind}).") from e
    aid = _upsert(conn, st["user_id"], "microsoft", me["email"] or "Microsoft account", json.dumps(tokens), "oauth_tokens",
                           external_account_id=me["email"], scopes=(tokens.get("scope") or "").split(),
                           config={"purposes": ["mail"]})
    return integration_view(conn, aid)


def set_mail_sending(conn, uid: str, box: str, enabled: bool) -> dict:
    """AJ turns sending on/off for one mailbox. On = a `mail_send` write grant (the executor requires it for every send,
    each of which still needs his explicit approval). Outlook additionally needs the Mail.Send consent."""
    if box not in A.MAILBOXES:
        raise SetupError("bad_mailbox", "mailbox must be gmail or outlook")
    acct = A.mail_account(conn, uid, box)
    if not acct:
        raise SetupError("not_connected", f"Connect {box} first.")
    if enabled and box == "outlook" and "Mail.Send" not in " ".join(acct["scopes"]):
        raise SetupError("needs_consent", "Reconnect Outlook with sending allowed (Microsoft needs the Mail.Send consent).")
    with tx(conn):
        A.set_grants(conn, acct["id"], "mail_send", "write",
                     [(acct["external_account_id"], acct["external_account_id"])] if enabled else [])
        audit(conn, "user", "mail.sending_" + ("enabled" if enabled else "disabled"), f"{box} sending "
              + ("enabled (every send still needs your approval)" if enabled else "disabled"), "integration_account", acct["id"],
              uid=uid)
    return integration_view(conn, acct["id"])


# ------------------------------------------------------------------ views
def integration_view(conn, account_id: str) -> dict:
    a = A.get_account_by_id(conn, account_id)
    grants = [{k: g[k] for k in ("resource_type", "resource_id", "resource_name", "access")} | {"is_default_target": bool(g["is_default_target"])}
              for g in A.grants(conn, account_id)]
    if a["provider"] == "ics":
        grants = []
    live = row(conn, "SELECT 1 FROM verification_receipts r JOIN proposed_actions p ON p.id=r.action_id WHERE p.account_id=? "
                     "AND r.ok=1 AND r.simulated=0 LIMIT 1", (account_id,)) is not None
    return {"provider": a["provider"], "account_id": a["id"], "status": a["status"], "label": a["label"], "scopes": a["scopes"],
            "config": {k: v for k, v in a["config"].items() if k in ("purposes",)}, "last_sync_at": a["last_sync_at"],
            "last_error": a["last_error"], "grants": grants, "live_verified": live}


def list_integrations(conn, uid: str) -> list[dict]:
    """The user's own connections plus org-level ones (Anthropic key, Telegram bot)."""
    out = [integration_view(conn, r["id"]) for r in rows(conn, "SELECT id FROM integration_accounts WHERE status!='revoked' "
                                                              "AND (user_id=? OR user_id IS NULL) ORDER BY provider", (uid,))]
    present = {o["provider"] for o in out}
    s = get_settings()
    for p in ("todoist", "google", "gmail", "microsoft", "ics", "github", "anthropic"):
        if p not in present:
            out.append({"provider": p, "account_id": None, "status": "not_connected", "label": None, "scopes": [], "grants": [],
                        "last_sync_at": None, "last_error": None, "live_verified": False,
                        "config": {"env_key": bool(s.anthropic_api_key)} if p == "anthropic" else {}})
    return out
