"""Connection flows and sync jobs for integrations (policy + persistence around the raw adapters)."""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
from datetime import timedelta

from jarvis.config import get_settings
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, iso, now_iso, parse_iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError


class SetupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ------------------------------------------------------------------ Todoist
def connect_todoist(conn, token: str) -> dict:
    from jarvis.integrations.todoist import TodoistAdapter
    token = token.strip()
    if len(token) < 20:
        raise SetupError("bad_token", "That doesn't look like a Todoist API token.")
    try:
        me = TodoistAdapter(token).whoami()
    except ProviderError as e:
        raise SetupError(e.kind, f"Todoist rejected the token ({e.kind}).") from e
    aid = A.upsert_account(conn, "todoist", me.get("email") or me.get("full_name") or "Todoist", token, "api_token",
                           external_account_id=str(me.get("id")), scopes=["data:read_write (personal token)"])
    return integration_view(conn, aid)


def todoist_projects(conn) -> list[dict]:
    tp = A.task_provider(conn)
    return [{"id": str(p["id"]), "name": p.get("name")} for p in tp.list_projects()]


def set_todoist_target(conn, project_id: str, area_projects: dict | None = None) -> dict:
    acct = A.get_account(conn, "todoist")
    if not acct:
        raise SetupError("not_connected", "Connect Todoist first.")
    projects = {p["id"]: p["name"] for p in todoist_projects(conn)}
    if project_id not in projects:
        raise SetupError("unknown_project", "That project isn't in your Todoist account.")
    chosen = {project_id} | set((area_projects or {}).values())
    unknown = [p for p in chosen if p not in projects]
    if unknown:
        raise SetupError("unknown_project", f"Unknown Todoist project(s): {unknown}")
    with tx(conn):
        A.set_grants(conn, acct["id"], "todoist_project", "write", [(p, projects[p]) for p in chosen], default_target=project_id)
        if area_projects is not None:
            r = row(conn, "SELECT data FROM user_settings WHERE id=1")
            data = jload(r["data"], {})
            data.setdefault("todoist", {})["area_projects"] = area_projects
            conn.execute("UPDATE user_settings SET data=?, updated_at=? WHERE id=1", (jdump(data), now_iso()))
    from jarvis.agents.specialists import refresh_open_destinations
    refresh_open_destinations(conn)
    return integration_view(conn, acct["id"])


def todoist_sync(conn) -> dict:
    """Pull remote state and reconcile Jarvis-created tasks (completed/deleted/edited externally)."""
    tp = A.task_provider(conn)
    remote = {t.id: t for t in tp.list_tasks()}
    changed = 0
    for tl in rows(conn, "SELECT * FROM task_links WHERE status IN ('active','pending_sync','unknown') AND external_id IS NOT NULL"):
        t = remote.get(tl["external_id"]) or tp.get_task(tl["external_id"])
        new_status = "deleted" if t is None or t.is_deleted else "completed" if t.is_completed else "active"
        if new_status != tl["status"] or (t and t.content != tl["content"]):
            conn.execute("UPDATE task_links SET status=?, content=COALESCE(?, content), last_synced_at=?, updated_at=? WHERE id=?",
                         (new_status, t.content if t else None, now_iso(), now_iso(), tl["id"]))
            changed += 1
    acct = A.get_account(conn, "todoist")
    if acct and not A.is_simulated(tp):
        A.mark_synced(conn, acct["id"])
    from jarvis.agents import context
    context.invalidate()
    return {"pulled": len(remote), "reconciled": changed}


# ------------------------------------------------------------------ Google OAuth
def google_setup_info() -> dict:
    from jarvis.integrations.google_oauth import CALENDAR_SCOPES, DRIVE_FILE_SCOPES, DRIVE_SCOPES
    s = get_settings()
    return {"configured": bool(s.google_client_id and s.google_client_secret), "redirect_uri": redirect_uri(),
            "required_apis": ["Google Calendar API", "Google Drive API (only if using Drive)"],
            "scopes": {"calendar": CALENDAR_SCOPES, "drive": DRIVE_SCOPES, "drive_per_file_alternative": DRIVE_FILE_SCOPES},
            "steps": [
                "Google Cloud Console → create a project (personal account; no Workspace admin / domain-wide delegation).",
                "APIs & Services → enable Google Calendar API (and Drive API if wanted).",
                "OAuth consent screen → External, Testing mode, add your own Google account as a test user; add the scopes listed.",
                "Credentials → Create OAuth client ID → Web application → Authorized redirect URI exactly: " + redirect_uri(),
                "Put JARVIS_GOOGLE_CLIENT_ID and JARVIS_GOOGLE_CLIENT_SECRET in .env (never commit), restart Jarvis, then Connect.",
            ],
            "drive_tradeoff": "drive.readonly lets Jarvis read files you select by ID/folder (Jarvis itself restricts reads to "
                              "the roots you allow); drive.file only covers files opened/created with this app and is NOT "
                              "arbitrary read access to existing files."}


def redirect_uri() -> str:
    s = get_settings()
    base = s.public_origin or f"http://{'localhost' if s.host in ('127.0.0.1', 'localhost') else s.host}:{s.port}"
    return base.rstrip("/") + "/api/integrations/google/callback"


def google_start(conn, purpose: str) -> tuple[str, str]:
    """Returns (auth_url, browser_binding). The caller sets the binding as a short-lived Lax cookie."""
    from jarvis.integrations.google_oauth import CALENDAR_SCOPES, DRIVE_SCOPES, build_auth_url, generate_pkce_pair
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret):
        raise SetupError("not_configured", "Google OAuth client isn't configured; follow the setup steps first.")
    if purpose not in ("calendar", "drive"):
        raise SetupError("bad_purpose", "purpose must be calendar or drive")
    state = pysecrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    binding = pysecrets.token_urlsafe(32)
    ru = redirect_uri()
    conn.execute("INSERT INTO oauth_states (state_hash, provider, purpose, code_verifier, redirect_uri, created_at, expires_at, "
                 "binding_hash) VALUES (?,?,?,?,?,?,?,?)",
                 (hashlib.sha256(state.encode()).hexdigest(), "google", purpose, verifier, ru, now_iso(),
                  iso(utcnow() + timedelta(minutes=10)), hashlib.sha256(binding.encode()).hexdigest()))
    scopes = CALENDAR_SCOPES if purpose == "calendar" else DRIVE_SCOPES
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
        tokens = exchange_code(s.google_client_id, s.google_client_secret, st["redirect_uri"], code, st["code_verifier"])
        info = fetch_userinfo(tokens["access_token"])
    except ProviderError as e:
        raise SetupError("token_exchange_failed", f"Google token exchange failed ({e.kind}).") from e
    existing = A.get_account(conn, "google")
    purposes = set(existing["config"].get("purposes", [])) if existing and existing["external_account_id"] == info.get("email") else set()
    purposes.add(st["purpose"])
    if existing and existing["external_account_id"] == info.get("email"):
        old = json.loads(A.read_secret(existing) or "{}")
        if not tokens.get("refresh_token") and old.get("refresh_token"):
            tokens["refresh_token"] = old["refresh_token"]
    granted = (tokens.get("scope") or "").split()
    aid = A.upsert_account(conn, "google", info.get("email") or "Google account", json.dumps(tokens), "oauth_tokens",
                           external_account_id=info.get("email"), scopes=granted, config={"purposes": sorted(purposes)})
    return integration_view(conn, aid)


def google_calendars(conn) -> list[dict]:
    cp = A.calendar_provider(conn)
    acct = A.get_account(conn, "google")
    reads = {g["resource_id"] for g in A.grants(conn, acct["id"], "calendar", "read")} if acct else set()
    tgt = A.default_target(conn, acct["id"], "calendar") if acct else None
    return [{"id": c["id"], "summary": c.get("summary"), "accessRole": c.get("accessRole"), "primary": c.get("primary", False),
             "timeZone": c.get("timeZone"), "selected_read": c["id"] in reads,
             "write_target": bool(tgt and tgt["resource_id"] == c["id"])} for c in cp.list_calendars()]


def set_calendars(conn, read_ids: list[str], write_target: str | None) -> dict:
    acct = A.get_account(conn, "google")
    if not acct:
        raise SetupError("not_connected", "Connect Google Calendar first.")
    cals = {c["id"]: c for c in A.calendar_provider(conn).list_calendars()}
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
    refresh_open_destinations(conn)
    return integration_view(conn, acct["id"])


# ------------------------------------------------------------------ Drive (selected roots, read-only)
def set_drive_roots(conn, ids: list[str]) -> dict:
    acct = A.get_account(conn, "google")
    if not acct or "drive" not in acct["config"].get("purposes", []):
        raise SetupError("not_connected", "Connect Google Drive first.")
    dp = A.drive_provider(conn)
    items = []
    for i in ids:
        meta = dp.get_file(i)
        items.append((i, meta.get("name"), meta.get("mimeType")))
    with tx(conn):
        A.set_grants(conn, acct["id"], "drive_folder", "read", [(i, n) for i, n, m in items if m == "application/vnd.google-apps.folder"])
        A.set_grants(conn, acct["id"], "drive_file", "read", [(i, n) for i, n, m in items if m != "application/vnd.google-apps.folder"])
    return integration_view(conn, acct["id"])


def drive_allowed(conn, file_meta: dict) -> bool:
    acct = A.get_account(conn, "google")
    if not acct:
        return False
    files = {g["resource_id"] for g in A.grants(conn, acct["id"], "drive_file", "read")}
    folders = {g["resource_id"] for g in A.grants(conn, acct["id"], "drive_folder", "read")}
    return file_meta["id"] in files or bool(set(file_meta.get("parents") or []) & folders)


def drive_ingest(conn, file_id: str) -> dict:
    """Fetch (or refresh if changed) a selected Drive file into the local evidence store."""
    from jarvis.knowledge.ingest import ingest_bytes
    dp = A.drive_provider(conn)
    meta = dp.get_file(file_id)
    if not drive_allowed(conn, meta):
        raise SetupError("not_selected", "That Drive file isn't inside a folder/file you selected for Jarvis.")
    data, mime = dp.download(file_id, meta["mimeType"], 20 * 1024 * 1024)
    name = meta.get("name", file_id)
    if mime == "text/plain" and not name.endswith(".txt"):
        name += ".txt"
    elif mime == "application/pdf" and not name.endswith(".pdf"):
        name += ".pdf"
    elif mime == "text/csv" and not name.endswith(".csv"):
        name += ".csv"
    res = ingest_bytes(conn, data, name, mime, None, origin="drive", origin_ref=file_id)
    return res | {"remote_modified": meta.get("modifiedTime"), "link": meta.get("webViewLink")}


# ------------------------------------------------------------------ ICS deadline feed
def connect_ics(conn, url: str) -> dict:
    from jarvis.security.netguard import UnsafeURL, validate_url
    try:
        clean = validate_url(url.strip())
    except UnsafeURL as e:
        raise SetupError("unsafe_url", str(e)) from e
    aid = A.upsert_account(conn, "ics", "Deadline feed", clean, "ics_url", external_account_id="ics-feed", scopes=["read"])
    return integration_view(conn, aid)


def ics_refresh(conn, fetcher=None) -> dict:
    from jarvis.agents import context
    from jarvis.knowledge import ics
    from jarvis.security.netguard import UnsafeURL, safe_get
    acct = A.get_account(conn, "ics")
    if not acct:
        raise SetupError("not_connected", "No deadline feed configured.")
    url = A.read_secret(acct)
    try:
        body = fetcher(url) if fetcher else safe_get(url).content
    except (UnsafeURL, Exception) as e:  # never log the URL
        A.set_status(conn, acct["id"], "error", f"Feed refresh failed: {type(e).__name__}")
        raise SetupError("fetch_failed", f"Feed refresh failed ({type(e).__name__}); last good data kept.") from e
    today = context.local_today(conn)
    ws, we = today - timedelta(days=7), today + timedelta(days=90)
    try:
        items = ics.parse_feed(body, ws, we, context.user_tz(conn))
    except Exception as e:
        A.set_status(conn, acct["id"], "error", f"Feed could not be parsed: {type(e).__name__}")
        raise SetupError("parse_failed", "Feed could not be parsed; last good data kept.") from e
    keys: set[str] = set()
    try:
        tz = context.user_tz(conn)
        cal = context.events_between(conn, context.day_bounds(ws, tz)[0], context.day_bounds(we, tz)[1])
        for e in cal["events"]:
            d = e["start"][:10] if e["all_day"] else parse_iso(e["start"]).astimezone(__import__("zoneinfo").ZoneInfo(tz)).date().isoformat()
            keys.add(ics.calendar_key(e["summary"] or "", d))
    except Exception:
        pass
    with tx(conn):
        counts = ics.upsert_deadlines(conn, acct["id"], items, frozenset(keys), window_start=ws, window_end=we)
        A.mark_synced(conn, acct["id"])
        audit(conn, "worker", "ics.refreshed", f"Deadline feed refreshed: {counts}", "integration_account", acct["id"])
    return counts | {"items": len(items), "last_refresh": now_iso()}


# ------------------------------------------------------------------ GitHub (read-only)
def connect_github(conn, token: str, repos: list[str]) -> dict:
    from jarvis.integrations.github import GitHubAdapter
    gh = GitHubAdapter(token.strip())
    for r in repos:
        try:
            gh.repo_status(r)
        except ProviderError as e:
            raise SetupError(e.kind, f"Can't read {r}: {e.message}") from e
    aid = A.upsert_account(conn, "github", "GitHub (read-only)", token.strip(), "fine_grained_pat", external_account_id="github",
                           scopes=["metadata:read", "issues:read", "pull_requests:read"])
    A.set_grants(conn, aid, "github_repo", "read", [(r, r) for r in repos])
    return integration_view(conn, aid)


def github_status(conn) -> list[dict]:
    acct = A.get_account(conn, "github")
    if not acct:
        return []
    gh = A.github_provider(conn)
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
    aid = A.upsert_account(conn, "anthropic", "Anthropic API", key, "api_key", external_account_id="anthropic")
    return integration_view(conn, aid) | {"note": "Key stored encrypted. No test call is made until you enable a budget."}


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


def list_integrations(conn) -> list[dict]:
    out = [integration_view(conn, r["id"]) for r in rows(conn, "SELECT id FROM integration_accounts WHERE status!='revoked' "
                                                              "ORDER BY provider")]
    present = {o["provider"] for o in out}
    s = get_settings()
    for p in ("todoist", "google", "ics", "github", "anthropic"):
        if p not in present:
            out.append({"provider": p, "account_id": None, "status": "not_connected", "label": None, "scopes": [], "grants": [],
                        "last_sync_at": None, "last_error": None, "live_verified": False,
                        "config": {"env_key": bool(s.anthropic_api_key)} if p == "anthropic" else {}})
    return out
