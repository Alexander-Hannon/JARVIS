"""Second factors: authenticator-app codes (TOTP, RFC 6238), passkeys (WebAuthn), and one-time recovery codes.

Policy (800-171 3.5.3): owners, admins and approvers always need a second factor; with `JARVIS_MFA_POLICY=all` (the
default in remote mode) everyone does. A user who must have MFA but hasn't enrolled can sign in with their password
only to enroll — every other route answers 403 `mfa_enrollment_required` until they finish.

TOTP secrets are sealed with the app key; a code's time step is remembered so the same code can't be replayed.
WebAuthn stores only the public key. Recovery codes are stored hashed and work once each.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote, urlsplit

from fastapi import HTTPException, Request

from jarvis.config import get_settings
from jarvis.core.db import row, rows
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow
from jarvis.security.principal import PRIVILEGED
from jarvis.security.secrets import seal, unseal

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1                 # accept the previous/next step for clock drift
CHALLENGE_TTL = timedelta(minutes=5)
RECOVERY_CODES = 10
ISSUER = "JARVIS"


# ---------------------------------------------------------------- policy
def policy() -> str:
    """off | privileged | all. `auto` (default) means all in remote mode, privileged on a loopback-only install."""
    s = get_settings()
    p = (s.mfa_policy or "auto").lower()
    if p == "auto":
        return "all" if s.remote_mode else "privileged"
    return p if p in ("off", "privileged", "all") else "privileged"


def required(role: str) -> bool:
    p = policy()
    return p == "all" or (p == "privileged" and role in PRIVILEGED)


def methods(conn, uid: str) -> list[dict]:
    """Usable (confirmed) second factors."""
    return rows(conn, "SELECT id, kind, label, created_at, last_used_at FROM user_mfa WHERE user_id=? AND "
                      "(kind='webauthn' OR confirmed_at IS NOT NULL) ORDER BY created_at", (uid,))


def enrolled(conn, uid: str) -> bool:
    return bool(methods(conn, uid))


# ---------------------------------------------------------------- TOTP (RFC 6238, HMAC-SHA1, 6 digits, 30 s)
def totp_at(secret_b32: str, step: int) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()   # noqa: S324 — RFC 6238 mandates SHA-1 HMAC
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_match(secret_b32: str, code: str, now: float | None = None, last_step: int | None = None) -> int | None:
    """The matching time step, or None. Steps at or before `last_step` are refused (no replay)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != TOTP_DIGITS:
        return None
    cur = int((now if now is not None else time.time()) // TOTP_STEP)
    for step in range(cur - TOTP_WINDOW, cur + TOTP_WINDOW + 1):
        if (last_step is None or step > last_step) and hmac.compare_digest(totp_at(secret_b32, step), code):
            return step
    return None


def totp_begin(conn, uid: str, username: str, label: str = "Authenticator app") -> dict:
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    mid = new_id("mfa")
    conn.execute("DELETE FROM user_mfa WHERE user_id=? AND kind='totp' AND confirmed_at IS NULL", (uid,))
    conn.execute("INSERT INTO user_mfa (id, user_id, kind, label, secret_sealed, created_at) VALUES (?,?,?,?,?,?)",
                 (mid, uid, "totp", label[:60] or "Authenticator app", seal(secret), now_iso()))
    uri = (f"otpauth://totp/{quote(ISSUER)}:{quote(username)}?secret={secret}&issuer={quote(ISSUER)}"
           f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")
    return {"id": mid, "secret": secret, "otpauth_uri": uri}


def totp_confirm(conn, uid: str, mid: str, code: str) -> bool:
    m = row(conn, "SELECT * FROM user_mfa WHERE id=? AND user_id=? AND kind='totp' AND confirmed_at IS NULL", (mid, uid))
    if not m:
        raise HTTPException(404, "Not found.")
    step = totp_match(unseal(m["secret_sealed"]), code)
    if step is None:
        return False
    conn.execute("UPDATE user_mfa SET confirmed_at=?, last_used_at=?, last_step=? WHERE id=?", (now_iso(), now_iso(), step, mid))
    audit(conn, "user", "mfa.enrolled", "Authenticator app added", "user", uid, uid=uid)
    return True


def _verify_totp(conn, uid: str, code: str) -> bool:
    for m in rows(conn, "SELECT * FROM user_mfa WHERE user_id=? AND kind='totp' AND confirmed_at IS NOT NULL", (uid,)):
        step = totp_match(unseal(m["secret_sealed"]), code, last_step=m["last_step"])
        if step is not None:
            conn.execute("UPDATE user_mfa SET last_used_at=?, last_step=? WHERE id=?", (now_iso(), step, m["id"]))
            return True
    return False


# ---------------------------------------------------------------- recovery codes
def _rc_hash(code: str) -> str:
    return hashlib.sha256("".join(ch for ch in code.upper() if ch.isalnum()).encode()).hexdigest()


def new_recovery_codes(conn, uid: str) -> list[str]:
    """Replaces any previous set. Shown to the user once."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)) for _ in range(RECOVERY_CODES)]
    conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id=?", (uid,))
    for c in codes:
        conn.execute("INSERT INTO mfa_recovery_codes (id, user_id, code_hash, created_at) VALUES (?,?,?,?)",
                     (new_id("rc"), uid, _rc_hash(c), now_iso()))
    audit(conn, "user", "mfa.recovery_codes", "New recovery codes generated", "user", uid, uid=uid)
    return codes


def recovery_left(conn, uid: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM mfa_recovery_codes WHERE user_id=? AND used_at IS NULL", (uid,)).fetchone()[0]


def _use_recovery(conn, uid: str, code: str) -> bool:
    r = row(conn, "SELECT id FROM mfa_recovery_codes WHERE user_id=? AND code_hash=? AND used_at IS NULL", (uid, _rc_hash(code)))
    if not r:
        return False
    conn.execute("UPDATE mfa_recovery_codes SET used_at=? WHERE id=?", (now_iso(), r["id"]))
    audit(conn, "user", "mfa.recovery_used", f"Recovery code used ({recovery_left(conn, uid)} left)", "user", uid, uid=uid)
    return True


# ---------------------------------------------------------------- WebAuthn (passkeys)
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def relying_party(request: Request) -> tuple[str, str]:
    """(rp_id, origin) for the address the person is actually browsing. Only JARVIS's own allowed origins qualify, so
    a passkey is bound to this install's host (localhost and the HTTPS front each get their own)."""
    from jarvis.api.app import allowed_origins
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin not in allowed_origins():
        raise HTTPException(400, "Passkeys need a request from JARVIS's own address.")
    return urlsplit(origin).hostname or "localhost", origin


def webauthn_register_begin(conn, request: Request, uid: str, username: str, display: str, label: str) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    rp_id, _origin = relying_party(request)
    existing = [PublicKeyCredentialDescriptor(id=_unb64u(m["credential_id"])) for m in
                rows(conn, "SELECT credential_id FROM user_mfa WHERE user_id=? AND kind='webauthn'", (uid,))]
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=ISSUER, user_name=username, user_id=uid.encode(), user_display_name=display,
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                               user_verification=UserVerificationRequirement.PREFERRED))
    cid = new_id("mch")
    conn.execute("INSERT INTO mfa_challenges (id, user_id, purpose, challenge, label, created_at, expires_at) "
                 "VALUES (?,?,?,?,?,?,?)", (cid, uid, "webauthn_register", _b64u(opts.challenge), label[:60] or "Passkey",
                                            now_iso(), iso(utcnow() + CHALLENGE_TTL)))
    import json
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def _take_challenge(conn, cid: str, purpose: str, uid: str | None = None) -> dict:
    c = row(conn, "SELECT * FROM mfa_challenges WHERE id=? AND purpose=? AND used_at IS NULL "
                  "AND (? IS NULL OR user_id=?)", (cid, purpose, uid or None, uid or None))
    if not c or parse_iso(c["expires_at"]) < utcnow():
        raise HTTPException(401, "That sign-in step expired; start again.")
    conn.execute("UPDATE mfa_challenges SET used_at=? WHERE id=?", (now_iso(), cid))
    return c


def webauthn_register_finish(conn, request: Request, uid: str, cid: str, credential: dict) -> dict:
    from webauthn import verify_registration_response
    from webauthn.helpers.exceptions import InvalidRegistrationResponse
    c = _take_challenge(conn, cid, "webauthn_register", uid)
    rp_id, origin = relying_party(request)
    try:
        v = verify_registration_response(credential=credential, expected_challenge=_unb64u(c["challenge"]),
                                         expected_rp_id=rp_id, expected_origin=origin)
    except InvalidRegistrationResponse as e:
        raise HTTPException(400, f"The passkey couldn't be verified ({e}).") from None
    mid = new_id("mfa")
    conn.execute("INSERT INTO user_mfa (id, user_id, kind, label, credential_id, public_key, sign_count, confirmed_at, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (mid, uid, "webauthn", c["label"] or "Passkey", _b64u(v.credential_id), _b64u(v.credential_public_key),
                  v.sign_count, now_iso(), now_iso()))
    audit(conn, "user", "mfa.enrolled", f"Passkey added ({c['label'] or 'Passkey'})", "user", uid, uid=uid)
    return {"id": mid}


def webauthn_login_options(conn, request: Request, uid: str) -> dict | None:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor
    creds = rows(conn, "SELECT credential_id FROM user_mfa WHERE user_id=? AND kind='webauthn'", (uid,))
    if not creds:
        return None
    try:
        rp_id, _ = relying_party(request)
    except HTTPException:
        return None
    opts = generate_authentication_options(rp_id=rp_id, allow_credentials=[
        PublicKeyCredentialDescriptor(id=_unb64u(c["credential_id"])) for c in creds])
    import json
    return {"challenge": _b64u(opts.challenge), "options": json.loads(options_to_json(opts))}


def _verify_webauthn(conn, request: Request, uid: str, challenge_b64: str, credential: dict) -> bool:
    from webauthn import verify_authentication_response
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse
    cred_id = credential.get("rawId") or credential.get("id") or ""
    m = row(conn, "SELECT * FROM user_mfa WHERE user_id=? AND kind='webauthn' AND credential_id=?", (uid, cred_id))
    if not m or not challenge_b64:
        return False
    rp_id, origin = relying_party(request)
    try:
        v = verify_authentication_response(credential=credential, expected_challenge=_unb64u(challenge_b64),
                                           expected_rp_id=rp_id, expected_origin=origin,
                                           credential_public_key=_unb64u(m["public_key"]),
                                           credential_current_sign_count=m["sign_count"])
    except InvalidAuthenticationResponse:
        return False
    conn.execute("UPDATE user_mfa SET sign_count=?, last_used_at=? WHERE id=?", (v.new_sign_count, now_iso(), m["id"]))
    return True


# ---------------------------------------------------------------- the login step
def start_login(conn, request: Request, user: dict) -> dict:
    """Password was right and the user has a second factor: hand back a short-lived challenge (no session yet)."""
    cid = new_id("mch")
    wa = webauthn_login_options(conn, request, user["id"])
    conn.execute("INSERT INTO mfa_challenges (id, user_id, purpose, challenge, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                 (cid, user["id"], "login", wa["challenge"] if wa else None, now_iso(), iso(utcnow() + CHALLENGE_TTL)))
    kinds = {m["kind"] for m in methods(conn, user["id"])}
    return {"mfa_required": True, "challenge_id": cid,
            "methods": sorted(kinds) + (["recovery"] if recovery_left(conn, user["id"]) else []),
            "webauthn": wa["options"] if wa else None}


def challenge_user(conn, cid: str) -> dict | None:
    return row(conn, "SELECT c.*, u.username FROM mfa_challenges c JOIN users u ON u.id=c.user_id "
                     "WHERE c.id=? AND c.purpose='login'", (cid,))


def finish_login(conn, request: Request, cid: str, method: str, code: str | None, credential: dict | None) -> dict:
    """Returns the user row when the second factor checks out (the caller creates the session)."""
    c = _take_challenge(conn, cid, "login")
    uid = c["user_id"]
    ok = (method == "totp" and _verify_totp(conn, uid, code or "")) or \
         (method == "recovery" and _use_recovery(conn, uid, code or "")) or \
         (method == "webauthn" and credential is not None and _verify_webauthn(conn, request, uid, c["challenge"], credential))
    if not ok:
        raise HTTPException(401, "That code or passkey didn't check out.")
    u = row(conn, "SELECT * FROM users WHERE id=? AND status='active'", (uid,))
    if not u:
        raise HTTPException(401, "Invalid credentials.")
    return u


def remove_method(conn, uid: str, role: str, mid: str) -> None:
    m = row(conn, "SELECT * FROM user_mfa WHERE id=? AND user_id=?", (mid, uid))
    if not m:
        raise HTTPException(404, "Not found.")
    if required(role) and len(methods(conn, uid)) <= 1 and (m["kind"] == "webauthn" or m["confirmed_at"]):
        raise HTTPException(400, "This is your only second factor and your role requires one. Add another first.")
    conn.execute("DELETE FROM user_mfa WHERE id=?", (mid,))
    audit(conn, "user", "mfa.removed", f"Second factor removed ({m['label']})", "user", uid, uid=uid)


def reset_user(conn, uid: str, by: str) -> None:
    """Admin: wipe someone's second factors (lost phone). Their sessions end; they enroll again at next sign-in."""
    conn.execute("DELETE FROM user_mfa WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id=?", (uid,))
    conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), uid))
    who = "from the local CLI" if by == uid else "by an admin"
    audit(conn, "user", "mfa.reset", f"Second factors reset {who}; sessions ended", "user", uid, uid=by)
