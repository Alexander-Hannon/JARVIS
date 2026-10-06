"""Authentication: first-run setup, username/password login, invites, pairing codes, sessions, CSRF, throttles.

One org per database, many users (see `security/principal.py` for roles). There is no default password and no
unauthenticated mode. First-run setup (which creates the first *owner*) is accepted from a *direct* loopback client
(the machine running Jarvis, no proxy in between, local profile only) or with a one-time setup token printed on that
machine's console. Behind an HTTPS front every request arrives from 127.0.0.1, so a forwarded request is never
"loopback" and the remote profile always requires the token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request

from jarvis.config import get_settings
from jarvis.core.db import row
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow

SESSION_COOKIE = "jarvis_session"
CSRF_HEADER = "x-csrf-token"

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)
# Constant-time-ish dummy so an unknown username costs the same scrypt work as a wrong password.
_DUMMY_HASH = "scrypt$AAAAAAAAAAAAAAAAAAAAAA==$" + base64.b64encode(b"\0" * 32).decode()

# Throttles (per client IP and per username, persisted in auth_attempts). Documented in
# docs/security/NIST_800-171_MAPPING.md (3.1.8).
WINDOW = timedelta(minutes=15)
IP_FAILURE_LIMIT = {"login": 10, "pair": 5, "setup": 10, "invite": 10, "mfa": 10}
# Per-username failures in the window from IPs that never signed in as that user: a distributed guess against one
# account. Devices that signed in as that user before can keep trying, so nobody is locked out of their own devices.
USER_FAILURE_LIMIT = 10
# Across all accounts (spraying one password over many usernames): only known IPs may continue.
ACCOUNT_FAILURE_LIMIT = 30
PAIR_GLOBAL_FAILURE_LIMIT = 30
KNOWN_IP_DAYS = 30
SETUP_TOKEN_TTL = timedelta(hours=24)
INVITE_TTL = timedelta(hours=72)
MIN_PASSWORD = 12
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str | None) -> bool:
    try:
        _, salt_b64, dk_b64 = (stored or _DUMMY_HASH).split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return stored is not None and hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters.")


def normalize_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise HTTPException(400, "Username: 2–32 characters, lowercase letters, digits, '.', '_' or '-'.")
    return u


def owner_exists(conn) -> bool:
    return row(conn, "SELECT id FROM users WHERE role='owner' AND status='active' LIMIT 1") is not None


def create_owner(conn, username: str, password: str, display_name: str | None = None) -> str:
    """First-run only: the first user is the owner. Returns the user id."""
    check_password_rules(password)
    if row(conn, "SELECT id FROM users LIMIT 1"):
        raise HTTPException(409, "Owner already configured.")
    uname = normalize_username(username)
    uid = "usr_owner"     # stable id for the first owner (personal installs, migrations and backups rely on it)
    conn.execute("INSERT INTO users (id, username, display_name, password_hash, role, created_at) VALUES (?,?,?,?,?,?)",
                 (uid, uname, (display_name or uname).strip()[:80], hash_password(password), "owner", now_iso()))
    audit(conn, "user", "auth.setup", f"Owner account created ({uname})", "user", uid, uid=uid)
    return uid


def create_session(conn, user_id: str, label: str | None = None, request: Request | None = None,
                   auth_level: str = "password") -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    sid = new_id("ses")
    exp = utcnow() + timedelta(hours=get_settings().session_ttl_hours)
    ip = client_ip(request) if request else None
    ua = (request.headers.get("user-agent") or "")[:200] if request else None
    conn.execute(
        "INSERT INTO sessions (id, token_hash, csrf_token, label, created_at, expires_at, last_seen_at, ip, user_agent, "
        "user_id, auth_level) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, _token_hash(token), csrf, label, now_iso(), iso(exp), now_iso(), ip, ua, user_id, auth_level),
    )
    return sid, token, csrf


def check_credentials(conn, username: str, password: str, request: Request | None = None) -> dict:
    """Verify username + password for an active user and return the user row. The caller records the attempt
    (in its own transaction, so failures persist) and decides whether a second factor is still needed."""
    u = row(conn, "SELECT * FROM users WHERE username=? COLLATE NOCASE", ((username or "").strip(),))
    ok = verify_password(password, u["password_hash"] if u else None)
    if not u or not ok or u["status"] != "active":
        raise HTTPException(401, "Invalid credentials.")
    dormant = get_settings().dormant_days
    last = u["last_login_at"] or u["created_at"]
    if dormant and u["role"] != "owner" and utcnow() - parse_iso(last) > timedelta(days=dormant):
        # 800-171 3.5.6: identifiers are disabled after inactivity; an admin re-enables them.
        conn.execute("UPDATE users SET status='disabled', disabled_at=? WHERE id=?", (now_iso(), u["id"]))
        audit(conn, "system", "user.dormant_disabled", f"{u['username']} disabled after {dormant} days without sign-in",
              "user", u["id"])
        raise HTTPException(401, "Invalid credentials.")
    return u


def complete_login(conn, user: dict, request: Request | None, label: str | None = None,
                   auth_level: str = "password") -> tuple[str, str, str]:
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
    audit(conn, "user", "auth.login", f"{user['username']} signed in", data=request_context(request) | {"auth": auth_level},
          uid=user["id"])
    return create_session(conn, user["id"], label, request, auth_level)


def create_pairing_code(conn, user_id: str) -> str:
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    conn.execute("INSERT INTO pairing_codes (id, code_hash, created_at, expires_at, user_id) VALUES (?,?,?,?,?)",
                 (new_id("pair"), _token_hash(code), now_iso(), iso(utcnow() + timedelta(minutes=10)), user_id))
    audit(conn, "user", "auth.pairing_code", "Pairing code issued (10 min)", uid=user_id)
    return code


def redeem_pairing_code(conn, code: str, label: str | None, request: Request | None = None) -> tuple[str, str, str]:
    r = row(conn, "SELECT p.*, u.status FROM pairing_codes p JOIN users u ON u.id=p.user_id "
                  "WHERE p.code_hash=? AND p.used_at IS NULL", (_token_hash(code.strip().upper()),))
    if not r or parse_iso(r["expires_at"]) < utcnow() or r["status"] != "active":
        raise HTTPException(401, "Invalid or expired pairing code.")
    conn.execute("UPDATE pairing_codes SET used_at=? WHERE id=?", (now_iso(), r["id"]))
    audit(conn, "user", "auth.paired", f"Device paired: {label or 'unnamed'}", data=request_context(request),
          uid=r["user_id"])
    # The code was issued by that user's own signed-in session, so the paired device inherits its assurance (M1a MFA:
    # pairing codes are only issued from MFA-verified sessions).
    return create_session(conn, r["user_id"], label or "paired device", request, auth_level="device")


# ---------------------------------------------------------------- invites
def create_invite(conn, user_id: str, created_by: str) -> str:
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE invites SET used_at=? WHERE user_id=? AND used_at IS NULL", (now_iso(), user_id))
    conn.execute("INSERT INTO invites (id, token_hash, user_id, created_by, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                 (new_id("inv"), _token_hash(token), user_id, created_by, now_iso(), iso(utcnow() + INVITE_TTL)))
    return token


def invite_user(conn, token: str) -> dict:
    r = row(conn, "SELECT i.id AS invite_id, i.expires_at, u.* FROM invites i JOIN users u ON u.id=i.user_id "
                  "WHERE i.token_hash=? AND i.used_at IS NULL", (_token_hash(token),))
    if not r or parse_iso(r["expires_at"]) < utcnow() or r["status"] != "active":
        raise HTTPException(401, "This invite link is invalid or has expired; ask your admin for a new one.")
    return r


def accept_invite(conn, token: str, password: str, request: Request | None) -> tuple[str, str, str]:
    u = invite_user(conn, token)
    check_password_rules(password)
    conn.execute("UPDATE invites SET used_at=? WHERE id=?", (now_iso(), u["invite_id"]))
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), u["id"]))
    conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), u["id"]))
    audit(conn, "user", "auth.invite_accepted", f"{u['username']} set a password from an invite",
          data=request_context(request), uid=u["id"])
    return complete_login(conn, u, request, "invite")


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
    s = row(conn, "SELECT s.*, u.username, u.display_name, u.role, u.status AS user_status FROM sessions s "
                  "JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL", (_token_hash(token),))
    if not s or parse_iso(s["expires_at"]) < utcnow() or s["user_status"] != "active":
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


def check_throttle(conn, kind: str, request: Request, username: str | None = None) -> None:
    ip = client_ip(request)
    fails = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind=? AND ip=? AND ok=0 AND at>=?",
                         (kind, ip, _since(WINDOW))).fetchone()[0]
    if fails >= IP_FAILURE_LIMIT[kind]:
        raise HTTPException(429, "Too many attempts from this device; wait 15 minutes.")
    if kind in ("login", "mfa"):
        uname = (username or "").strip().lower() or None
        known = conn.execute("SELECT 1 FROM auth_attempts WHERE kind='login' AND ip=? AND ok=1 AND at>=? "
                             "AND (? IS NULL OR username=?)",
                             (ip, _since(timedelta(days=KNOWN_IP_DAYS)), uname, uname)).fetchone()
        if uname and not known:
            per_user = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind IN ('login','mfa') AND username=? "
                                    "AND ok=0 AND at>=?", (uname, _since(WINDOW))).fetchone()[0]
            if per_user >= USER_FAILURE_LIMIT:
                raise HTTPException(429, "Sign-in for this account is limited to devices that have used it before; "
                                         "wait 15 minutes.")
        total = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind='login' AND ok=0 AND at>=?",
                             (_since(WINDOW),)).fetchone()[0]
        if total >= ACCOUNT_FAILURE_LIMIT and not known:
            raise HTTPException(429, "Sign-in is temporarily limited to devices that have signed in before.")
    if kind == "pair":
        total = conn.execute("SELECT COUNT(*) FROM auth_attempts WHERE kind='pair' AND ok=0 AND at>=?",
                             (_since(WINDOW),)).fetchone()[0]
        if total >= PAIR_GLOBAL_FAILURE_LIMIT:
            raise HTTPException(429, "Pairing is paused for 15 minutes after repeated wrong codes.")


def record_attempt(conn, kind: str, request: Request, ok: bool, username: str | None = None) -> None:
    """Called in its own transaction so a failed attempt is kept even though the request itself errors."""
    ip = client_ip(request)
    uname = (username or "").strip().lower()[:64] or None
    conn.execute("INSERT INTO auth_attempts (at, kind, ip, ok, username) VALUES (?,?,?,?,?)",
                 (now_iso(), kind, ip, int(ok), uname))
    conn.execute("DELETE FROM auth_attempts WHERE at<?", (_since(timedelta(days=KNOWN_IP_DAYS + 1)),))
    if not ok:
        audit(conn, "system", f"auth.{kind}_failed", f"Failed {kind} attempt",
              data=request_context(request) | ({"username": uname} if uname else {}))


def check_csrf(request: Request, session: dict) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    # Bearer-token clients (CLI) are not subject to ambient-cookie CSRF.
    if not request.cookies.get(SESSION_COOKIE):
        return
    sent = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(sent, session["csrf_token"]):
        raise HTTPException(403, "CSRF token missing or invalid.")
