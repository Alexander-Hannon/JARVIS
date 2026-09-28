"""Single-owner authentication: one-time local setup, password login, pairing codes, sessions, CSRF.

There is no default password and no unauthenticated mode. Initial setup is only accepted from a
loopback client (the machine running Jarvis) unless a setup token printed on the console is supplied.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request

from jarvis.config import get_settings
from jarvis.core.db import row
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow

SESSION_COOKIE = "jarvis_session"
CSRF_HEADER = "x-csrf-token"

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


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
    if len(password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters.")
    if owner_exists(conn):
        raise HTTPException(409, "Owner already configured.")
    conn.execute("INSERT INTO owner (id, password_hash, created_at) VALUES (1,?,?)", (hash_password(password), now_iso()))
    audit(conn, "user", "auth.setup", "Owner account created")


def create_session(conn, label: str | None = None) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    sid = new_id("ses")
    exp = utcnow() + timedelta(hours=get_settings().session_ttl_hours)
    conn.execute(
        "INSERT INTO sessions (id, token_hash, csrf_token, label, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (sid, _token_hash(token), csrf, label, now_iso(), iso(exp)),
    )
    return sid, token, csrf


def login(conn, password: str, label: str | None = None) -> tuple[str, str, str]:
    o = row(conn, "SELECT password_hash FROM owner WHERE id=1")
    if not o or not verify_password(password, o["password_hash"]):
        audit(conn, "system", "auth.login_failed", "Failed login attempt")
        raise HTTPException(401, "Invalid credentials.")
    audit(conn, "user", "auth.login", "Owner signed in")
    return create_session(conn, label)


def create_pairing_code(conn) -> str:
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    conn.execute("INSERT INTO pairing_codes (id, code_hash, created_at, expires_at) VALUES (?,?,?,?)",
                 (new_id("pair"), _token_hash(code), now_iso(), iso(utcnow() + timedelta(minutes=10))))
    audit(conn, "user", "auth.pairing_code", "Pairing code issued (10 min)")
    return code


def redeem_pairing_code(conn, code: str, label: str | None) -> tuple[str, str, str]:
    r = row(conn, "SELECT * FROM pairing_codes WHERE code_hash=? AND used_at IS NULL", (_token_hash(code.strip().upper()),))
    if not r or parse_iso(r["expires_at"]) < utcnow():
        raise HTTPException(401, "Invalid or expired pairing code.")
    conn.execute("UPDATE pairing_codes SET used_at=? WHERE id=?", (now_iso(), r["id"]))
    audit(conn, "user", "auth.paired", f"Device paired: {label or 'unnamed'}")
    return create_session(conn, label or "paired device")


def session_from_request(conn, request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    if not token:
        return None
    s = row(conn, "SELECT * FROM sessions WHERE token_hash=? AND revoked_at IS NULL", (_token_hash(token),))
    if not s or parse_iso(s["expires_at"]) < utcnow():
        return None
    conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now_iso(), s["id"]))
    return s


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def check_csrf(request: Request, session: dict) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    # Bearer-token clients (CLI) are not subject to ambient-cookie CSRF.
    if not request.cookies.get(SESSION_COOKIE):
        return
    sent = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(sent, session["csrf_token"]):
        raise HTTPException(403, "CSRF token missing or invalid.")
