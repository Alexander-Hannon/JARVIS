"""Single-owner authentication: one-time setup, password login, pairing codes, sessions, CSRF, throttles.

There is no default password and no unauthenticated mode. Initial setup is accepted from a *direct* loopback
client (the machine running Jarvis, no proxy in between, local profile only) or with a one-time setup token that
is printed on that machine's console. Behind an HTTPS front every request arrives from 127.0.0.1, so a forwarded
request is never "loopback" and the remote profile always requires the token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import ipaddress
from datetime import timedelta

from fastapi import HTTPException, Request

from jarvis.config import get_settings
from jarvis.core.db import row
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow

SESSION_COOKIE = "jarvis_session"
CSRF_HEADER = "x-csrf-token"

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)

# Throttles (per client IP, persisted in auth_attempts). Window and limits are deliberately simple and documented in
# docs/security/NIST_800-171_MAPPING.md (3.1.8).
WINDOW = timedelta(minutes=15)
IP_FAILURE_LIMIT = {"login": 10, "pair": 5, "setup": 10}
# When this many logins fail across all IPs in the window (a distributed guess), only IPs that have signed in
# successfully before may keep trying, so the owner is never locked out of his own devices.
ACCOUNT_FAILURE_LIMIT = 30
PAIR_GLOBAL_FAILURE_LIMIT = 30
KNOWN_IP_DAYS = 30
SETUP_TOKEN_TTL = timedelta(hours=24)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def owner_exists(conn) -> bool:
    return row(conn, "SELECT id FROM owner WHERE id=1") is not None


def create_owner(conn, password: str) -> None:
    if len(password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters.")
    if owner_exists(conn):
        raise HTTPException(409, "Owner already configured.")
    conn.execute("INSERT INTO owner (id, password_hash, created_at) VALUES (1,?,?)", (hash_password(password), now_iso()))
    audit(conn, "user", "auth.setup", "Owner account created")


def create_session(conn, label: str | None = None, request: Request | None = None) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    sid = new_id("ses")
    exp = utcnow() + timedelta(hours=get_settings().session_ttl_hours)
    ip = client_ip(request) if request else None
    ua = (request.headers.get("user-agent") or "")[:200] if request else None
    conn.execute(
        "INSERT INTO sessions (id, token_hash, csrf_token, label, created_at, expires_at, last_seen_at, ip, user_agent) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, _token_hash(token), csrf, label, now_iso(), iso(exp), now_iso(), ip, ua),
    )
    return sid, token, csrf


def login(conn, password: str, label: str | None = None, request: Request | None = None) -> tuple[str, str, str]:
    """Verify the owner password. The caller records the attempt (in its own transaction, so failures persist)."""
    o = row(conn, "SELECT password_hash FROM owner WHERE id=1")
    if not o or not verify_password(password, o["password_hash"]):
        raise HTTPException(401, "Invalid credentials.")
    audit(conn, "user", "auth.login", "Owner signed in", data=request_context(request))
    return create_session(conn, label, request)


def create_pairing_code(conn) -> str:
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    conn.execute("INSERT INTO pairing_codes (id, code_hash, created_at, expires_at) VALUES (?,?,?,?)",
                 (new_id("pair"), _token_hash(code), now_iso(), iso(utcnow() + timedelta(minutes=10))))
    audit(conn, "user", "auth.pairing_code", "Pairing code issued (10 min)")
    return code


def redeem_pairing_code(conn, code: str, label: str | None, request: Request | None = None) -> tuple[str, str, str]:
    r = row(conn, "SELECT * FROM pairing_codes WHERE code_hash=? AND used_at IS NULL", (_token_hash(code.strip().upper()),))
    if not r or parse_iso(r["expires_at"]) < utcnow():
        raise HTTPException(401, "Invalid or expired pairing code.")
    conn.execute("UPDATE pairing_codes SET used_at=? WHERE id=?", (now_iso(), r["id"]))
    audit(conn, "user", "auth.paired", f"Device paired: {label or 'unnamed'}", data=request_context(request))
    return create_session(conn, label or "paired device", request)


def _request_token(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    return token or None


def session_from_request(conn, request: Request) -> dict | None:
    token = _request_token(request)
    if not token:
        return None
    s = row(conn, "SELECT * FROM sessions WHERE token_hash=? AND revoked_at IS NULL", (_token_hash(token),))
    if not s or parse_iso(s["expires_at"]) < utcnow():
        return None
    last = parse_iso(s["last_seen_at"] or s["created_at"])
    if utcnow() - last > timedelta(hours=get_settings().session_idle_hours):
        # Idle sessions end for good (not just this request) so a stolen-but-stale token stays dead.
        conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now_iso(), s["id"]))
        audit(conn, "system", "auth.session_idle", "Session ended after inactivity", "session", s["id"])
        return None
    conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now_iso(), s["id"]))
    return s


def revoke_request_session(conn, request: Request) -> None:
    """Login/pair rotate: any session the browser already carried is revoked so a planted token can't ride along."""
    token = _request_token(request)
    if token:
        conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                     (now_iso(), _token_hash(token)))


# ---------------------------------------------------------------- client identity
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _forwarded(request: Request) -> bool:
    return any(h in request.headers for h in _FORWARD_HEADERS)


def _valid_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """The client address for throttling and audit. X-Forwarded-For is believed only from a configured trusted proxy;
    the right-most hop that isn't itself a trusted proxy is the client (left-most entries are client-controlled)."""
    peer = _peer(request)
    trusted = set(get_settings().trusted_proxies)
    if peer in trusted and (xff := request.headers.get("x-forwarded-for")):
        for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
            if hop not in trusted:
                return hop if _valid_ip(hop) else peer
    return peer or "unknown"


def is_loopback(request: Request) -> bool:
    """A direct connection from this machine. A proxied request (any forwarding header) never counts."""
    return _peer(request) in ("127.0.0.1", "::1") and not _forwarded(request)


def setup_allowed_without_token(request: Request) -> bool:
    return not get_settings().remote_mode and is_loopback(request)


def request_context(request: Request | None) -> dict:
    if request is None:
        return {}
    return {"ip": client_ip(request), "user_agent": (request.headers.get("user-agent") or "")[:200]}


# ---------------------------------------------------------------- setup tokens
def issue_setup_token(conn) -> str:
    """A fresh one-time token for first-run setup; any earlier unused token stops working."""
    token = "-".join(secrets.token_hex(3).upper() for _ in range(4))
    conn.execute("DELETE FROM setup_tokens WHERE used_at IS NULL")
    conn.execute("INSERT INTO setup_tokens (token_hash, created_at, expires_at) VALUES (?,?,?)",
                 (_token_hash(_norm_setup(token)), now_iso(), iso(utcnow() + SETUP_TOKEN_TTL)))
    audit(conn, "system", "auth.setup_token", "Setup token issued on the console (24 h)")
    return token


def _norm_setup(token: str) -> str:
    return "".join(ch for ch in token.upper() if ch.isalnum())


def consume_setup_token(conn, token: str) -> bool:
    r = row(conn, "SELECT * FROM setup_tokens WHERE token_hash=? AND used_at IS NULL", (_token_hash(_norm_setup(token)),))
    if not r or parse_iso(r["expires_at"]) < utcnow():
        return False
    conn.execute("UPDATE setup_tokens SET used_at=? WHERE token_hash=?", (now_iso(), r["token_hash"]))
    return True


# ---------------------------------------------------------------- throttles
def _since(delta: timedelta) -> str:
    return iso(utcnow() - delta)


def check_throttle(conn, kind: str, request: Request) -> None:
    ip = client_ip(request)
    fails = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind=? AND ip=? AND ok=0 AND at>=?",
                         (kind, ip, _since(WINDOW))).fetchone()[0]
    if fails >= IP_FAILURE_LIMIT[kind]:
        raise HTTPException(429, "Too many attempts from this device; wait 15 minutes.")
    if kind == "login":
        total = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind='login' AND ok=0 AND at>=?",
                             (_since(WINDOW),)).fetchone()[0]
        known = conn.execute("SELECT 1 FROM auth_attempts WHERE kind='login' AND ip=? AND ok=1 AND at>=?",
                             (ip, _since(timedelta(days=KNOWN_IP_DAYS)))).fetchone()
        if total >= ACCOUNT_FAILURE_LIMIT and not known:
            raise HTTPException(429, "Sign-in is temporarily limited to devices that have signed in before.")
    if kind == "pair":
        total = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind='pair' AND ok=0 AND at>=?",
                             (_since(WINDOW),)).fetchone()[0]
        if total >= PAIR_GLOBAL_FAILURE_LIMIT:
            raise HTTPException(429, "Pairing is paused for 15 minutes after repeated wrong codes.")


def record_attempt(conn, kind: str, request: Request, ok: bool) -> None:
    """Called in its own transaction so a failed attempt is kept even though the request itself errors."""
    ip = client_ip(request)
    conn.execute("INSERT INTO auth_attempts (at, kind, ip, ok) VALUES (?,?,?,?)", (now_iso(), kind, ip, int(ok)))
    conn.execute("DELETE FROM auth_attempts WHERE at<?", (_since(timedelta(days=KNOWN_IP_DAYS + 1)),))
    if not ok:
        audit(conn, "system", f"auth.{kind}_failed", f"Failed {kind} attempt", data=request_context(request))


def check_csrf(request: Request, session: dict) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    # Bearer-token clients (CLI) are not subject to ambient-cookie CSRF.
    if not request.cookies.get(SESSION_COOKIE):
        return
    sent = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(sent, session["csrf_token"]):
        raise HTTPException(403, "CSRF token missing or invalid.")
