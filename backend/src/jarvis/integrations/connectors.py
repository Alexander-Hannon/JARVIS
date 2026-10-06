"""Connector catalog: one declarative entry per V4 app integration.

The Settings → Integrations app cards, the Connections gallery and the Help page all render from this catalog, so
what the UI says JARVIS can do is what the code registers. Each connector declares its auth fields, setup steps, what
it reads, which action types it can write (tiers come from actions/registry.py), and an optional resource picker
(the application-layer allowlist on top of the provider's own permissions).
"""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from jarvis.config import get_settings
from jarvis.core.db import row, tx
from jarvis.core.util import iso, now_iso, parse_iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError
from jarvis.security.secrets import seal, unseal
from jarvis.integrations.service import SetupError, _upsert, integration_view, redirect_uri


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class Resource:
    type: str                     # scope_grants.resource_type
    label: str                    # "Pages JARVIS may write under"
    access: tuple[str, ...]       # ("write",) or ("read", "write")
    lister: Callable              # lister(conn, uid) -> [{id, name, detail?}]


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    family: str                   # notes | messaging | school | life | home | travel | finance
    auth: str                     # token | oauth
    summary: str
    reads: tuple[str, ...]
    writes: tuple[str, ...] = ()
    fields: tuple[Field, ...] = ()
    steps: tuple[str, ...] = ()
    resource: Resource | None = None
    connect: Callable | None = field(default=None, compare=False)


# ------------------------------------------------------------------------------------------------ connect handlers
def _bad(kind: str, vendor: str, e: ProviderError) -> SetupError:
    return SetupError(e.kind, f"{vendor} rejected the {kind} ({e.kind}).")


def _connect_notion(conn, uid: str, v: dict) -> str:
    from jarvis.integrations.notion import NotionAdapter
    tok = (v.get("token") or "").strip()
    if len(tok) < 20:
        raise SetupError("bad_token", "That doesn't look like a Notion integration secret.")
    try:
        me = NotionAdapter(tok).whoami()
    except ProviderError as e:
        raise _bad("secret", "Notion", e) from None
    return _upsert(conn, uid, "notion", me.get("workspace") or me["name"], tok, "api_token",
                            external_account_id=me["id"], scopes=["pages shared with the integration"])


def _connect_slack(conn, uid: str, v: dict) -> str:
    from jarvis.integrations.slack import USER_SCOPES, SlackAdapter
    tok = (v.get("token") or "").strip()
    if not tok.startswith("xoxp-"):
        raise SetupError("bad_token", "Use a Slack *user* token (starts with xoxp-) so JARVIS sees what you see.")
    try:
        me = SlackAdapter(tok).whoami()
    except ProviderError as e:
        raise _bad("token", "Slack", e) from None
    aid = _upsert(conn, uid, "slack", f"{me.get('user')} @ {me.get('team')}", tok, "user_token",
                           external_account_id=f"{me.get('team')}:{me.get('user_id')}", scopes=USER_SCOPES,
                           config={"user_id": me.get("user_id")})
    # Posting into AJ's own DM is always allowed (it reaches nobody else); other channels need explicit grants.
    A.set_grants(conn, aid, "slack_self", "write", [(me.get("user_id") or "self", "Your own DM")])
    return aid


def _connect_canvas(conn, uid: str, v: dict) -> str:
    from jarvis.integrations.canvas import CanvasAdapter
    from jarvis.security.netguard import UnsafeURL, validate_url
    base, tok = (v.get("base_url") or "").strip().rstrip("/"), (v.get("token") or "").strip()
    try:
        validate_url(base)
    except UnsafeURL as e:
        raise SetupError("unsafe_url", str(e)) from None
    try:
        me = CanvasAdapter(base, tok).whoami()
    except ProviderError as e:
        raise _bad("token", "Canvas", e) from None
    return _upsert(conn, uid, "canvas", f"{me.get('name')} ({base.split('//')[1]})", tok, "api_token",
                            external_account_id=f"{base}:{me.get('id')}", scopes=["read-only"], config={"base_url": base})


def _connect_ha(conn, uid: str, v: dict) -> str:
    from jarvis.integrations.homeassistant import HomeAssistantAdapter, UnsafeHAURL, validate_base_url
    try:
        base = validate_base_url(v.get("base_url") or "")
    except UnsafeHAURL as e:
        raise SetupError("unsafe_url", str(e)) from None
    tok = (v.get("token") or "").strip()
    try:
        HomeAssistantAdapter(base, tok).ping()
    except ProviderError as e:
        raise _bad("token", "Home Assistant", e) from None
    return _upsert(conn, uid, "homeassistant", f"Home Assistant ({base.split('//')[1]})", tok, "api_token",
                            external_account_id=base, scopes=["allowlisted entities only"], config={"base_url": base})


def _connect_routing(conn, uid: str, v: dict) -> str:
    from jarvis.integrations.routing import RoutingAdapter
    key = (v.get("api_key") or "").strip()
    try:
        RoutingAdapter(key).geocode("Huntsville, AL")
    except ProviderError as e:
        raise _bad("key", "OpenRouteService", e) from None
    return _upsert(conn, uid, "routing", "OpenRouteService", key, "api_key", external_account_id=f"ors:{uid}", scopes=["read"])


def _connect_simplefin(conn, uid: str, v: dict) -> str:
    from jarvis.integrations.simplefin import SetupTokenError, SimpleFinAdapter, claim, claim_url_from_token
    try:
        access = claim(claim_url_from_token(v.get("setup_token") or ""))
        SimpleFinAdapter(access).accounts()
    except SetupTokenError as e:
        raise SetupError("bad_token", str(e)) from None
    except ProviderError as e:
        raise _bad("access", "SimpleFIN", e) from None
    return _upsert(conn, uid, "simplefin", "Bank accounts (SimpleFIN, read-only)", access, "access_url",
                            external_account_id=f"simplefin:{uid}", scopes=["read-only"])


# ------------------------------------------------------------------------------------------------ resource listers
def _notion_pages(conn, uid: str) -> list[dict]:
    return [{"id": p["id"], "name": p["title"], "detail": p.get("url")} for p in A.app_provider(conn, uid, "notion").search("", 50)]


def _slack_channels(conn, uid: str) -> list[dict]:
    return [{"id": c["id"], "name": ("#" if not c["is_im"] else "") + c["name"], "detail": "private" if c["is_private"] else ""}
            for c in A.app_provider(conn, uid, "slack").channels() if not c["is_im"]]


def _ha_entities(conn, uid: str) -> list[dict]:
    from jarvis.integrations.homeassistant import is_secure
    return [{"id": s["entity_id"], "name": s["name"], "detail": ("always asks" if is_secure(s["entity_id"]) else s["state"])}
            for s in A.app_provider(conn, uid, "homeassistant").states()]


CATALOG: dict[str, Connector] = {c.id: c for c in (
    Connector("notion", "Notion", "notes", "token", "Search and read pages you share with JARVIS; add notes under pages "
              "you choose.", ("Search pages", "Read a page"), ("notion.page.create", "notion.page.append"),
              (Field("token", "Internal integration secret", secret=True, placeholder="ntn_… / secret_…"),),
              ("notion.so/profile/integrations → New integration (Internal) → copy the secret.",
               "In Notion, open each page JARVIS may use → ••• → Connections → add your integration.",
               "Paste the secret here, then pick the pages JARVIS may write under."),
              Resource("notion_page", "Pages JARVIS may add notes under", ("write",), _notion_pages), _connect_notion),
    Connector("slack", "Slack", "messaging", "token", "Catch up on mentions and channels; post to your own DM; post "
              "elsewhere only with your explicit approval.", ("Search messages", "Mentions", "Channel history"),
              ("slack.post_self", "slack.post"),
              (Field("token", "User OAuth token", secret=True, placeholder="xoxp-…"),),
              ("api.slack.com/apps → Create app (from scratch) in your workspace.",
               "OAuth & Permissions → User Token Scopes: search:read, channels:history, groups:history, im:history, "
               "mpim:history, channels:read, groups:read, im:read, users:read, chat:write.",
               "Install to workspace → copy the User OAuth Token (xoxp-…)."),
              Resource("slack_channel", "Channels JARVIS may post in (each post still needs your approval)", ("write",),
                       _slack_channels), _connect_slack),
    Connector("canvas", "Canvas LMS", "school", "token", "Assignments, to-dos and announcements for your courses "
              "(read-only — JARVIS never submits or grades).", ("Courses", "Upcoming assignments", "To-dos", "Announcements"),
              (), (Field("base_url", "Canvas address", placeholder="https://uah.instructure.com"),
                   Field("token", "Access token", secret=True)),
              ("Canvas → Account → Settings → Approved integrations → + New access token.",
               "Paste your school's Canvas address and the token.")),
    Connector("spotify", "Spotify", "life", "oauth", "See what's playing; play, pause, skip and queue music (Premium).",
              ("Now playing", "Search music"), ("music.play", "music.pause", "music.next", "music.queue"), (),
              ("developer.spotify.com/dashboard → Create app (Web API).",
               "Redirect URI: exactly the one shown below (Spotify requires 127.0.0.1, not localhost).",
               "Put JARVIS_SPOTIFY_CLIENT_ID in .env, restart, then Connect while browsing JARVIS at that same address.")),
    Connector("homeassistant", "Home Assistant", "home", "token", "See and control the devices you allowlist. Locks, "
              "alarms, garage doors and scripts always need your explicit approval.", ("Device states",),
              ("home.control", "home.secure"),
              (Field("base_url", "Home Assistant address", placeholder="http://homeassistant.local:8123"),
               Field("token", "Long-lived access token", secret=True)),
              ("Home Assistant → your profile → Security → Long-lived access tokens → Create token.",
               "Paste the address and token, then tick the devices JARVIS may see and control."),
              Resource("ha_entity", "Devices JARVIS may see / control", ("read", "write"), _ha_entities), _connect_ha),
    Connector("routing", "Travel time (OpenRouteService)", "travel", "token", "Drive/walk/bike time to places and "
              "'leave by' heads-ups for events with a location.", ("Travel time", "Leave-by alerts"), (),
              (Field("api_key", "API key", secret=True),),
              ("openrouteservice.org → sign up (free) → Dashboard → copy your API key.",), None, _connect_routing),
    Connector("simplefin", "Bank accounts (SimpleFIN)", "finance", "token", "Balances, recent transactions and unusual "
              "charges. Read-only by design: no way to move money.", ("Balances", "Transactions", "Unusual charges"), (),
              (Field("setup_token", "SimpleFIN setup token", secret=True),),
              ("bridge.simplefin.org → connect your bank (SimpleFIN charges a small yearly fee).",
               "Create a setup token for an app and paste it here once; JARVIS exchanges it for read-only access."),
              None, _connect_simplefin),
)}
# Connectors whose connect handler is a plain token form (Spotify uses OAuth instead).
_CONNECT = {"canvas": _connect_canvas}
# Consumer connectors only offered by the personal edition (a company install doesn't connect employees' bank
# accounts, music or home devices).
PERSONAL_ONLY = frozenset({"canvas", "spotify", "homeassistant", "routing", "simplefin"})


def offered(cid: str) -> bool:
    return get_settings().edition == "personal" or cid not in PERSONAL_ONLY


def view(conn, uid: str, c: Connector) -> dict:
    acct = A.get_account(conn, uid, c.id)
    simulated = A._override(uid, c.id) is not None
    status = acct["status"] if acct else ("connected" if simulated else "not_connected")
    out = {"id": c.id, "name": c.name, "family": c.family, "auth": c.auth, "summary": c.summary, "reads": list(c.reads),
           "writes": list(c.writes), "fields": [f.__dict__ for f in c.fields], "steps": list(c.steps), "status": status,
           "simulated": simulated, "account": integration_view(conn, acct["id"]) if acct else None,
           "resource": {"type": c.resource.type, "label": c.resource.label, "access": list(c.resource.access)}
           if c.resource else None}
    if c.id == "spotify":
        s = get_settings()
        out["oauth"] = {"configured": bool(s.spotify_client_id), "redirect_uri": spotify_redirect_uri()}
    return out


def catalog(conn, uid: str) -> list[dict]:
    return [view(conn, uid, c) for c in CATALOG.values() if offered(c.id)]


def _get(cid: str) -> Connector:
    c = CATALOG.get(cid)
    if not c or not offered(cid):
        raise SetupError("unknown_connector", "That connector isn't available on this install.")
    return c


def connect(conn, uid: str, cid: str, values: dict) -> dict:
    c = _get(cid)
    fn = c.connect or _CONNECT.get(cid)
    if not fn:
        raise SetupError("unknown_connector", "That connector can't be connected with a token form.")
    with tx(conn):
        fn(conn, uid, values)
    return view(conn, uid, c)


def resources(conn, uid: str, cid: str) -> dict:
    c = _get(cid)
    if not c.resource:
        raise SetupError("no_resources", "This connector has no resource picker.")
    acct = A.get_account(conn, uid, cid)
    if not acct and A._override(uid, cid) is None:
        raise SetupError("not_connected", f"Connect {c.name} first.")
    items = c.resource.lister(conn, uid)
    granted = {acc: {g["resource_id"] for g in A.grants(conn, acct["id"], c.resource.type, acc)} if acct else set()
               for acc in c.resource.access}
    return {"type": c.resource.type, "label": c.resource.label, "access": list(c.resource.access),
            "items": [i | {acc: i["id"] in granted[acc] for acc in c.resource.access} for i in items]}


def set_resources(conn, uid: str, cid: str, chosen: dict[str, list[str]]) -> dict:
    c = _get(cid)
    acct = A.get_account(conn, uid, cid)
    if not c.resource or not acct:
        raise SetupError("not_connected", f"Connect {c.name} first.")
    names = {i["id"]: i["name"] for i in c.resource.lister(conn, uid)}
    with tx(conn):
        for acc in c.resource.access:
            ids = chosen.get(acc) or []
            unknown = [i for i in ids if i not in names]
            if unknown:
                raise SetupError("unknown_resource", f"Unknown {c.resource.type}: {unknown[:3]}")
            if acc == "write" and "read" in c.resource.access:
                ids = [i for i in ids if i in set(chosen.get("read") or [])]   # can't control what JARVIS can't see
            A.set_grants(conn, acct["id"], c.resource.type, acc, [(i, names[i]) for i in ids])
    return resources(conn, uid, cid)


# ------------------------------------------------------------------------------------------------ Spotify OAuth (PKCE)
def spotify_redirect_uri() -> str:
    # Spotify rejects "localhost"; loopback must be an explicit IP.
    return redirect_uri().replace("://localhost", "://127.0.0.1").replace("/google/callback", "/spotify/callback")


def spotify_start(conn, uid: str) -> tuple[str, str]:
    from jarvis.integrations.google_oauth import generate_pkce_pair
    from jarvis.integrations.spotify import build_auth_url
    s = get_settings()
    if not s.spotify_client_id:
        raise SetupError("not_configured", "Set JARVIS_SPOTIFY_CLIENT_ID first (see the steps).")
    state, binding = pysecrets.token_urlsafe(32), pysecrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    ru = spotify_redirect_uri()
    conn.execute("INSERT INTO oauth_states (state_hash, provider, purpose, code_verifier, redirect_uri, created_at, expires_at, "
                 "binding_hash, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                 (hashlib.sha256(state.encode()).hexdigest(), "spotify", "playback", seal(verifier), ru, now_iso(),
                  iso(utcnow() + timedelta(minutes=10)), hashlib.sha256(binding.encode()).hexdigest(), uid))
    return build_auth_url(s.spotify_client_id, ru, state, challenge), binding


def spotify_callback(conn, state: str, code: str | None, error: str | None, binding: str | None) -> dict:
    import hmac

    from jarvis.integrations.spotify import SCOPES, SpotifyAdapter, SpotifyCredentials, exchange_code
    h = hashlib.sha256((state or "").encode()).hexdigest()
    st = row(conn, "SELECT * FROM oauth_states WHERE state_hash=?", (h,))
    if not st or st["used_at"] or parse_iso(st["expires_at"]) < utcnow() or st["provider"] != "spotify":
        raise SetupError("bad_state", "OAuth state is invalid, reused or expired. Start the connection again.")
    if not binding or not hmac.compare_digest(hashlib.sha256(binding.encode()).hexdigest(), st["binding_hash"] or ""):
        raise SetupError("wrong_browser", "Finish the connection in the same browser (and address) where you clicked Connect.")
    conn.execute("UPDATE oauth_states SET used_at=? WHERE state_hash=?", (now_iso(), h))
    if error or not code:
        raise SetupError("denied", f"Spotify returned: {error or 'no code'}")
    s = get_settings()
    try:
        tokens = exchange_code(s.spotify_client_id, st["redirect_uri"], code, unseal(st["code_verifier"]))
        me = SpotifyAdapter(SpotifyCredentials(tokens, s.spotify_client_id)).whoami()
    except ProviderError as e:
        raise SetupError("token_exchange_failed", f"Spotify token exchange failed ({e.kind}).") from None
    uid = st["user_id"]
    aid = _upsert(conn, uid, "spotify", me.get("name") or "Spotify", json.dumps(tokens), "oauth_tokens",
                           external_account_id=me.get("id"), scopes=SCOPES, config={"product": me.get("product")})
    return integration_view(conn, aid)
