"""HTTP routes. See docs/API_CONTRACT.md. The UI is never the authority for approval/authorization:
every check lives in the services these handlers call."""
from __future__ import annotations

import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from jarvis.actions import executor
from jarvis.actions import proposals as P
from jarvis.agents import chief
from jarvis.agents import context as ctx
from jarvis.api.app import db, principal, principal_enrolling, require, require_session
from jarvis.config import get_settings
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, new_id, now_iso, record_latency
from jarvis.security import auth
from jarvis.security.principal import Principal

public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api")      # people who use JARVIS (permission "use")
common = APIRouter(prefix="/api")      # any signed-in role, including auditors


def _cookie(resp: Response, token: str, request: Request) -> None:
    s = get_settings()
    # Secure whenever the browser is on HTTPS (directly, or the configured public origin), not just in remote mode.
    secure = s.remote_mode or request.url.scheme == "https"
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="strict", secure=secure,
                    max_age=s.session_ttl_hours * 3600, path="/")


def _perr(e: P.ProposalError):
    raise HTTPException(e.status, {"code": e.code, "message": e.message})


# ================================================================ auth (public)
class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class LoginIn(PasswordIn):
    username: str = Field(min_length=1, max_length=64)


class SetupIn(LoginIn):
    display_name: str | None = Field(default=None, max_length=80)
    setup_token: str | None = Field(default=None, max_length=64)


class PairIn(BaseModel):
    code: str = Field(max_length=32)
    label: str | None = Field(default=None, max_length=80)


class InviteAcceptIn(PasswordIn):
    token: str = Field(min_length=10, max_length=128)


def me_view(s: dict) -> dict:
    return {"id": s["user_id"], "username": s["username"], "display_name": s["display_name"], "role": s["role"],
            "auth_level": s.get("auth_level") or "password"}


def _mfa_state(conn, s: dict) -> dict:
    from jarvis.security import mfa
    req = mfa.required(s["role"])
    enrolled = mfa.enrolled(conn, s["user_id"])
    return {"required": req, "enrolled": enrolled, "satisfied": (not req) or s.get("auth_level") in ("mfa", "device")}


@public.get("/auth/status")
def auth_status(request: Request, conn=Depends(db)):
    s = auth.session_from_request(conn, request)
    exists = auth.owner_exists(conn)
    return {"owner_exists": exists, "authenticated": bool(s), "csrf_token": s["csrf_token"] if s else None,
            "user": me_view(s) if s else None, "mfa": _mfa_state(conn, s) if s else None,
            "remote_mode": get_settings().remote_mode,
            "setup_allowed": (not exists) and auth.setup_allowed_without_token(request),
            "setup_token_required": (not exists) and not auth.setup_allowed_without_token(request)}


def _attempt(conn, kind: str, request: Request, fn, username: str | None = None):
    """Run an auth step under the per-IP/per-user throttle; the attempt is recorded even when the step fails."""
    with tx(conn):
        auth.check_throttle(conn, kind, request, username)
    try:
        with tx(conn):
            out = fn()
    except HTTPException as e:
        if e.status_code in (401, 403):
            with tx(conn):
                auth.record_attempt(conn, kind, request, ok=False, username=username)
        raise
    with tx(conn):
        auth.record_attempt(conn, kind, request, ok=True, username=username)
    return out


@public.post("/auth/setup")
def auth_setup(body: SetupIn, request: Request, response: Response, conn=Depends(db)):
    def step():
        if not auth.setup_allowed_without_token(request):
            if not body.setup_token or not auth.consume_setup_token(conn, body.setup_token):
                raise HTTPException(403, "Setup needs the one-time setup token printed in the console where "
                                         "`jarvis serve` runs (or run `jarvis setup-token` on that machine).")
        uid = auth.create_owner(conn, body.username, body.password, body.display_name)
        from jarvis.core.seed import seed_user
        seed_user(conn, uid, body.display_name or body.username)
        audit(conn, "user", "auth.setup_context", "Owner setup", data=auth.request_context(request), uid=uid)
        return auth.create_session(conn, uid, "setup", request)
    _, token, csrf = _attempt(conn, "setup", request, step)
    _cookie(response, token, request)
    return {"csrf_token": csrf}


@public.post("/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response, conn=Depends(db)):
    """Password first. If the account has a second factor, the answer is a short-lived MFA challenge (no session yet);
    POST /auth/mfa/verify finishes the sign-in."""
    from jarvis.security import mfa

    def step():
        user = auth.check_credentials(conn, body.username, body.password, request)
        if mfa.enrolled(conn, user["id"]):
            return mfa.start_login(conn, request, user)
        auth.revoke_request_session(conn, request)
        return auth.complete_login(conn, user, request)
    out = _attempt(conn, "login", request, step, username=body.username)
    if isinstance(out, dict):
        return out
    _, token, csrf = out
    _cookie(response, token, request)
    return {"csrf_token": csrf}


class MfaVerifyIn(BaseModel):
    challenge_id: str = Field(max_length=64)
    method: str = Field(pattern="^(totp|webauthn|recovery)$")
    code: str | None = Field(None, max_length=32)
    credential: dict[str, Any] | None = None


@public.post("/auth/mfa/verify")
def auth_mfa_verify(body: MfaVerifyIn, request: Request, response: Response, conn=Depends(db)):
    from jarvis.security import mfa
    ch = mfa.challenge_user(conn, body.challenge_id)
    uname = ch["username"] if ch else None

    def step():
        user = mfa.finish_login(conn, request, body.challenge_id, body.method, body.code, body.credential)
        auth.revoke_request_session(conn, request)
        return auth.complete_login(conn, user, request, auth_level="mfa")
    _, token, csrf = _attempt(conn, "mfa", request, step, username=uname)
    _cookie(response, token, request)
    return {"csrf_token": csrf}


@public.post("/auth/pair")
def auth_pair(body: PairIn, request: Request, response: Response, conn=Depends(db)):
    def step():
        auth.revoke_request_session(conn, request)
        return auth.redeem_pairing_code(conn, body.code, body.label, request)
    _, token, csrf = _attempt(conn, "pair", request, step)
    _cookie(response, token, request)
    return {"csrf_token": csrf}


@public.get("/auth/invite/{token}")
def invite_info(token: str, request: Request, conn=Depends(db)):
    with tx(conn):
        auth.check_throttle(conn, "invite", request)
    try:
        u = auth.invite_user(conn, token)
    except HTTPException:
        with tx(conn):
            auth.record_attempt(conn, "invite", request, ok=False)
        raise
    return {"username": u["username"], "display_name": u["display_name"], "expires_at": u["expires_at"]}


@public.post("/auth/invite/accept")
def invite_accept(body: InviteAcceptIn, request: Request, response: Response, conn=Depends(db)):
    def step():
        auth.revoke_request_session(conn, request)
        return auth.accept_invite(conn, body.token, body.password, request)
    _, token, csrf = _attempt(conn, "invite", request, step)
    _cookie(response, token, request)
    return {"csrf_token": csrf}


@public.get("/integrations/google/callback")
def google_callback(request: Request, state: str = "", code: str | None = None, error: str | None = None, conn=Depends(db)):
    # Google returns via a cross-site top-level redirect, so the SameSite=Strict session cookie is NOT sent here.
    # Authorization = unguessable single-use state (10 min, PKCE verifier held server-side) + the Lax binding
    # cookie that only the owner's authenticated session could have obtained from /google/start.
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import google_callback as cb
    resp_err = None
    try:
        cb(conn, state, code, error, request.cookies.get(OAUTH_BINDING_COOKIE))
        resp = RedirectResponse("/connections?connected=google", status_code=303)
    except SetupError as e:
        resp_err = e.code
        resp = RedirectResponse(f"/connections?connect_error={resp_err}", status_code=303)
    resp.delete_cookie(OAUTH_BINDING_COOKIE, path=OAUTH_CALLBACK_PATH)
    return resp


OAUTH_BINDING_COOKIE = "jarvis_oauth_binding"
OAUTH_CALLBACK_PATH = "/api/integrations/google/callback"


# ================================================================ auth (session; every role)
@common.get("/auth/me")
def auth_me(session=Depends(require_session)):
    return me_view(session)


@common.post("/auth/logout")
def auth_logout(response: Response, session=Depends(require_session), conn=Depends(db)):
    conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now_iso(), session["id"]))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@common.get("/auth/sessions")
def list_sessions(me: Principal = Depends(principal), conn=Depends(db)):
    """Your signed-in devices (NIST 3.1.12): what's connected, from where, and when it was last used."""
    out = rows(conn, "SELECT id, label, created_at, last_seen_at, expires_at, ip, user_agent FROM sessions "
                     "WHERE user_id=? AND revoked_at IS NULL AND expires_at>? "
                     "ORDER BY COALESCE(last_seen_at, created_at) DESC", (me.user_id, now_iso()))
    return [s | {"current": s["id"] == me.session_id} for s in out]


@common.post("/auth/sessions/{sid}/revoke")
def revoke_session(sid: str, request: Request, me: Principal = Depends(principal), conn=Depends(db)):
    with tx(conn):
        n = conn.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                         (now_iso(), sid, me.user_id)).rowcount
        if not n:
            raise HTTPException(404, "No such active session.")
        audit(conn, "user", "auth.session_revoked", "Device signed out from Settings", "session", sid,
              data=auth.request_context(request), uid=me.user_id)
    return {"ok": True, "current": sid == me.session_id}


@common.post("/auth/sessions/revoke-others")
def revoke_other_sessions(request: Request, me: Principal = Depends(principal), conn=Depends(db)):
    with tx(conn):
        n = conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
                         (now_iso(), me.user_id, me.session_id)).rowcount
        audit(conn, "user", "auth.sessions_revoked", f"Signed out {n} other device(s)", data=auth.request_context(request),
              uid=me.user_id)
    return {"revoked": n}


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


@common.post("/auth/password")
def change_password(body: PasswordChangeIn, request: Request, me: Principal = Depends(principal), conn=Depends(db)):
    with tx(conn):
        u = row(conn, "SELECT password_hash FROM users WHERE id=?", (me.user_id,))
        if not auth.verify_password(body.current_password, u["password_hash"] if u else None):
            raise HTTPException(403, "Current password is wrong.")
        auth.check_password_rules(body.new_password)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body.new_password), me.user_id))
        n = conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
                         (now_iso(), me.user_id, me.session_id)).rowcount
        audit(conn, "user", "auth.password_changed", f"Password changed; {n} other device(s) signed out",
              data=auth.request_context(request), uid=me.user_id)
    return {"ok": True, "signed_out": n}


# ---------------------------------------------------------------- second factors
class MfaLabelIn(BaseModel):
    label: str = Field("", max_length=60)


class TotpConfirmIn(BaseModel):
    id: str = Field(max_length=64)
    code: str = Field(max_length=12)


class WebauthnFinishIn(BaseModel):
    challenge_id: str = Field(max_length=64)
    credential: dict[str, Any]


def _after_enroll(conn, me: Principal) -> dict:
    """Proving a new factor upgrades this session; the first one also issues recovery codes (shown once)."""
    from jarvis.security import mfa
    conn.execute("UPDATE sessions SET auth_level='mfa' WHERE id=?", (me.session_id,))
    codes = mfa.new_recovery_codes(conn, me.user_id) if not mfa.recovery_left(conn, me.user_id) else None
    return {"ok": True, "recovery_codes": codes}


@common.get("/auth/mfa")
def mfa_overview(me: Principal = Depends(principal_enrolling), session=Depends(require_session), conn=Depends(db)):
    from jarvis.security import mfa
    return {**_mfa_state(conn, session), "policy": mfa.policy(), "methods": mfa.methods(conn, me.user_id),
            "recovery_codes_left": mfa.recovery_left(conn, me.user_id)}


@common.post("/auth/mfa/totp/begin")
def mfa_totp_begin(body: MfaLabelIn, me: Principal = Depends(principal_enrolling), conn=Depends(db)):
    from jarvis.security import mfa
    with tx(conn):
        return mfa.totp_begin(conn, me.user_id, me.username, body.label or "Authenticator app")


@common.post("/auth/mfa/totp/confirm")
def mfa_totp_confirm(body: TotpConfirmIn, request: Request, me: Principal = Depends(principal_enrolling), conn=Depends(db)):
    from jarvis.security import mfa
    with tx(conn):
        auth.check_throttle(conn, "mfa", request, me.username)
    with tx(conn):
        ok = mfa.totp_confirm(conn, me.user_id, body.id, body.code)
        if ok:
            return _after_enroll(conn, me)
    with tx(conn):
        auth.record_attempt(conn, "mfa", request, ok=False, username=me.username)
    raise HTTPException(400, "That code didn't match. Check the time on your phone and try the newest code.")


@common.post("/auth/mfa/webauthn/begin")
def mfa_webauthn_begin(body: MfaLabelIn, request: Request, me: Principal = Depends(principal_enrolling), conn=Depends(db)):
    from jarvis.security import mfa
    with tx(conn):
        return mfa.webauthn_register_begin(conn, request, me.user_id, me.username, me.display_name, body.label or "Passkey")


@common.post("/auth/mfa/webauthn/finish")
def mfa_webauthn_finish(body: WebauthnFinishIn, request: Request, me: Principal = Depends(principal_enrolling),
                        conn=Depends(db)):
    from jarvis.security import mfa
    with tx(conn):
        mfa.webauthn_register_finish(conn, request, me.user_id, body.challenge_id, body.credential)
        return _after_enroll(conn, me)


@common.post("/auth/mfa/recovery-codes")
def mfa_recovery_codes(me: Principal = Depends(principal), conn=Depends(db)):
    """New set (the old ones stop working). Needs a session that already passed MFA."""
    from jarvis.security import mfa
    if not mfa.enrolled(conn, me.user_id):
        raise HTTPException(400, "Add an authenticator app or passkey first.")
    with tx(conn):
        return {"recovery_codes": mfa.new_recovery_codes(conn, me.user_id)}


@common.delete("/auth/mfa/{mid}")
def mfa_remove(mid: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.security import mfa
    with tx(conn):
        mfa.remove_method(conn, me.user_id, me.role, mid)
    return {"ok": True}


@router.post("/auth/pairing-code")
def pairing_code(me: Principal = Depends(principal), conn=Depends(db)):
    code = auth.create_pairing_code(conn, me.user_id)
    return {"code": code, "expires_in_s": 600}


# ================================================================ conversations / voice / commands
@router.post("/conversations")
def new_conversation(me: Principal = Depends(principal), conn=Depends(db)):
    return {"id": chief.ensure_conversation(conn, me.user_id, None)}


@router.get("/conversations/{cid}/messages")
def conversation_messages(cid: str, me: Principal = Depends(principal), conn=Depends(db)):
    if not row(conn, "SELECT id FROM conversations WHERE id=? AND user_id=?", (cid, me.user_id)):
        raise HTTPException(404, "Not found.")
    return rows(conn, "SELECT id, role, text, spoken_text, capture_id, proposal_id, run_id, created_at FROM messages "
                      "WHERE conversation_id=? ORDER BY created_at", (cid,))


@router.get("/conversations")
def conversations(me: Principal = Depends(principal), conn=Depends(db)):
    return rows(conn, "SELECT id, title, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 30",
                (me.user_id,))


@router.get("/voice/status")
def voice_status(conn=Depends(db)):
    from jarvis.voice.stt import get_engine, hardware_profile
    lat = row(conn, "SELECT ms FROM latency_metrics WHERE kind='transcription' ORDER BY id DESC LIMIT 1")
    return get_engine().status() | {"hardware": hardware_profile(), "last_latency_ms": lat["ms"] if lat else None}


@router.post("/voice/warmup")
def voice_warmup(conn=Depends(db)):
    from jarvis.voice.stt import STTUnavailable, get_engine, hardware_profile
    try:
        st = get_engine().warm_up()
    except STTUnavailable as e:
        raise HTTPException(503, {"code": "stt_unavailable", "message": e.message}) from e
    record_latency(conn, "stt_warmup", st.get("warmup_ms") or 0)
    return st | {"hardware": hardware_profile()}


@router.get("/voice/tts/status")
def tts_status():
    from jarvis.voice.tts import get_tts
    return get_tts().status()


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    preset: str = "jarvis"
    rate: float = Field(1.0, ge=0.5, le=2.0)


@router.post("/voice/tts")
def tts(body: TTSIn, conn=Depends(db)):

    from jarvis.voice.tts import TTSUnavailable, get_tts
    try:
        wav = get_tts().synthesize(body.text, body.preset, body.rate)
    except TTSUnavailable as e:
        raise HTTPException(503, {"code": "tts_unavailable", "message": str(e)}) from e
    record_latency(conn, "tts", get_tts().last_ms or 0, {"preset": body.preset, "chars": len(body.text)})
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


class VoiceSessionIn(BaseModel):
    mode: str = "push_to_talk"
    conversation_id: str | None = None


@router.post("/voice/sessions")
def voice_session(body: VoiceSessionIn, me: Principal = Depends(principal), conn=Depends(db)):
    if body.mode not in ("push_to_talk", "hands_free"):
        raise HTTPException(400, "bad mode")
    cid = chief.ensure_conversation(conn, me.user_id, body.conversation_id)
    vid = new_id("vs")
    max_s = 600 if body.mode == "hands_free" else 3600
    conn.execute("INSERT INTO voice_sessions (id, user_id, conversation_id, mode, started_at, max_duration_s) "
                 "VALUES (?,?,?,?,?,?)", (vid, me.user_id, cid, body.mode, now_iso(), max_s))
    return {"id": vid, "max_duration_s": max_s, "conversation_id": cid}


@router.post("/voice/sessions/{vid}/end")
def voice_session_end(vid: str, me: Principal = Depends(principal), conn=Depends(db)):
    n = conn.execute("UPDATE voice_sessions SET status='ended', ended_at=? WHERE id=? AND user_id=?",
                     (now_iso(), vid, me.user_id)).rowcount
    if not n:
        raise HTTPException(404, "Not found.")
    return {"ok": True}


def _capture_view(conn, cap_id: str) -> dict:
    c = row(conn, "SELECT * FROM captures WHERE id=?", (cap_id,))
    tv = row(conn, "SELECT * FROM transcript_versions WHERE capture_id=? ORDER BY version DESC LIMIT 1", (cap_id,))
    return {"capture_id": c["id"], "status": c["status"], "captured_at": c["captured_at"], "tz": c["capture_tz"],
            "audio": {"mime": c["audio_mime"], "bytes": c["audio_bytes"], "duration_s": c["audio_duration_s"],
                      "retained": bool(c["audio_retained"]), "deleted_at": c["audio_deleted_at"]},
            "transcript": {k: tv[k] for k in ("version", "text", "source", "confidence", "latency_ms", "stt_model")} if tv else None,
            "error": {"code": c["error_code"], "message": c["error"]} if c["error_code"] else None}


@router.post("/captures/audio")
async def capture_audio(file: UploadFile = File(...), captured_at: str = Form(...), tz: str = Form("America/Chicago"),
                        conversation_id: str | None = Form(None), voice_session_id: str | None = Form(None),
                        retain_audio: str = Form("false"), me: Principal = Depends(principal), conn=Depends(db)):
    import anyio

    from jarvis.core.util import parse_iso
    from jarvis.voice import stt
    s = get_settings()
    t0 = time.perf_counter()
    data = await file.read(s.max_audio_bytes + 1)
    try:
        parse_iso(captured_at)
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception as e:
        raise HTTPException(400, "captured_at must be an ISO UTC timestamp and tz an IANA zone") from e
    cid = chief.ensure_conversation(conn, me.user_id, conversation_id)
    if voice_session_id and not row(conn, "SELECT id FROM voice_sessions WHERE id=? AND user_id=?",
                                    (voice_session_id, me.user_id)):
        voice_session_id = None
    cap_id = new_id("cap")
    conn.execute("INSERT INTO captures (id, user_id, conversation_id, voice_session_id, kind, captured_at, capture_tz, status, "
                 "audio_mime, audio_bytes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (cap_id, me.user_id, cid, voice_session_id, "audio", captured_at, tz, "transcribing", file.content_type,
                  len(data), now_iso()))
    record_latency(conn, "audio_upload", (time.perf_counter() - t0) * 1000, {"bytes": len(data)})

    def fail(code: str, msg: str, status: int = 422):
        conn.execute("UPDATE captures SET status='failed', error_code=?, error=?, audio_deleted_at=? WHERE id=?",
                     (code, msg, now_iso(), cap_id))
        raise HTTPException(status, {"code": code, "message": msg, "capture_id": cap_id})
    try:
        stt.check_mime(file.content_type)
        decoded = await anyio.to_thread.run_sync(stt.decode_audio, data)
        tr = await anyio.to_thread.run_sync(stt.get_engine().transcribe, decoded)
    except stt.STTUnavailable as e:
        fail("stt_unavailable", e.message, 503)
    except stt.AudioError as e:
        fail(e.code, e.message)
    from jarvis.core import settings as S
    retained = retain_audio.lower() == "true" and S.user_data(conn, me.user_id).get("privacy", {}).get("retain_audio", False)
    path = None
    if retained:
        ad = s.data_dir / "audio"
        ad.mkdir(exist_ok=True)
        path = str(ad / f"{cap_id}.bin")
        with open(path, "wb") as f:
            f.write(data)
    del data  # raw audio is not kept unless retention was explicitly opted into
    if not tr.text:
        fail("no_speech", "No speech detected in the recording.")
    conn.execute("UPDATE captures SET status='transcribed', audio_duration_s=?, audio_path=?, audio_retained=?, audio_deleted_at=? "
                 "WHERE id=?", (decoded.duration_s, path, int(retained), None if retained else now_iso(), cap_id))
    conn.execute("INSERT INTO transcript_versions (id, capture_id, version, text, source, stt_engine, stt_model, confidence, "
                 "latency_ms, created_at) VALUES (?,?,1,?, 'stt', 'faster-whisper', ?,?,?,?)",
                 (new_id("tv"), cap_id, tr.text, tr.model, tr.confidence, tr.latency_ms, now_iso()))
    record_latency(conn, "transcription", tr.latency_ms, {"audio_s": decoded.duration_s, "model": tr.model,
                                                          "format": decoded.source_format})
    return _capture_view(conn, cap_id) | {"conversation_id": cid, "audio_format": decoded.source_format}


class TranscriptEdit(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


@router.post("/captures/{cap_id}/transcript")
def edit_transcript(cap_id: str, body: TranscriptEdit, me: Principal = Depends(principal), conn=Depends(db)):
    with tx(conn):
        if not row(conn, "SELECT id FROM captures WHERE id=? AND user_id=?", (cap_id, me.user_id)):
            raise HTTPException(404, "capture not found")
        cur = row(conn, "SELECT MAX(version) v FROM transcript_versions WHERE capture_id=?", (cap_id,))
        if not cur or cur["v"] is None:
            raise HTTPException(404, "capture not found")
        conn.execute("INSERT INTO transcript_versions (id, capture_id, version, text, source, created_at) VALUES (?,?,?,?, 'user_edit', ?)",
                     (new_id("tv"), cap_id, cur["v"] + 1, body.text.strip(), now_iso()))
    return _capture_view(conn, cap_id)


class CommandIn(BaseModel):
    conversation_id: str | None = None
    text: str = Field("", max_length=5000)
    channel: str = "typed"
    capture_id: str | None = None
    transcript_version: int | None = None
    captured_at: str | None = None
    tz: str = "America/Chicago"
    speech_playing: bool = False
    attachments: list[str] = []
    client_run_id: str | None = Field(None, pattern=r"^run_[a-f0-9]{20}$")


@router.post("/commands")
def command(body: CommandIn, me: Principal = Depends(principal), conn=Depends(db)):
    if not body.capture_id and not body.text.strip():
        raise HTTPException(400, "Say or type something.")
    if body.channel not in ("voice", "typed"):
        raise HTTPException(400, "bad channel")
    inp = chief.CommandInput(user_id=me.user_id, conversation_id=body.conversation_id or "", text=body.text, channel=body.channel,
                             captured_at=body.captured_at or now_iso(), tz=body.tz, capture_id=body.capture_id,
                             transcript_version=body.transcript_version, speech_playing=body.speech_playing,
                             attachments=body.attachments, session_id=me.session_id, client_run_id=body.client_run_id)
    try:
        return chief.handle(conn, inp)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    r = row(conn, "SELECT status FROM runs WHERE id=? AND user_id=?", (run_id, me.user_id))
    if not r:
        raise HTTPException(404)
    conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
    note = "Run already finished; to stop an approved plan use Reject or Pause actions." if r["status"] != "running" else \
        "Cancellation requested; no new tool calls will start."
    return {"ok": True, "status": r["status"], "note": note}


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    if not row(conn, "SELECT id FROM runs WHERE id=? AND user_id=?", (run_id, me.user_id)):
        return []          # unknown or someone else's run: nothing to show (the UI polls before a run exists)
    out = rows(conn, "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
    for e in out:
        e["data"] = jload(e["data"], {})
    return out


# ================================================================ proposals / control
@router.get("/proposals")
def list_proposals(status: str = "active", me: Principal = Depends(principal), conn=Depends(db)):
    if status == "active":
        ids = rows(conn, "SELECT id FROM proposals WHERE user_id=? AND status IN "
                         "('proposed','queued','executing','unknown','partial','failed') ORDER BY updated_at DESC LIMIT 30",
                   (me.user_id,))
    else:
        ids = rows(conn, "SELECT id FROM proposals WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (me.user_id,))
    return [P.view(conn, me.user_id, r["id"]) for r in ids]


@router.get("/proposals/{pid}")
def get_proposal(pid: str, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        return P.view(conn, me.user_id, pid)
    except P.ProposalError as e:
        _perr(e)


class ApproveIn(BaseModel):
    version: int
    hash: str
    selected_items: list[int]


@router.post("/proposals/{pid}/approve")
def approve(pid: str, body: ApproveIn, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        res = P.approve(conn, me.user_id, pid, body.version, body.hash, body.selected_items, channel="button",
                        session_id=me.session_id)
    except P.ProposalError as e:
        _perr(e)
    return res | {"proposal": P.view(conn, me.user_id, pid)}


@router.post("/proposals/{pid}/reject")
def reject(pid: str, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        P.reject(conn, me.user_id, pid)
    except P.ProposalError as e:
        _perr(e)
    return P.view(conn, me.user_id, pid)


class CoApproveIn(BaseModel):
    version: int
    hash: str
    decision: str = Field("approve", pattern="^(approve|decline)$")


@router.get("/approvals/co")
def co_approvals(me: Principal = Depends(principal), conn=Depends(db)):
    """Org-impacting proposals from other people that you're allowed to co-approve (e.g. skill installs for admins)."""
    return P.pending_co_approvals(conn, me.user_id, me.can)


@router.post("/approvals/co/{pid}")
def co_approve(pid: str, body: CoApproveIn, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        return P.co_approve(conn, me.user_id, me.can, pid, body.version, body.hash, decision=body.decision,
                            session_id=me.session_id)
    except P.ProposalError as e:
        _perr(e)


class ReviseItem(BaseModel):
    index: int
    args: dict[str, Any] | None = None
    remove: bool = False


class ReviseIn(BaseModel):
    items: list[ReviseItem]


EDITABLE_ARGS = {"task.create": {"content", "description", "due_date", "due_datetime", "due_string", "priority", "labels",
                                 "duration", "duration_unit"},
                 "event.create": {"summary", "description", "start", "end"},
                 "task.update": {"content", "description", "due_date", "due_datetime", "priority"},
                 "file.write": {"path", "content"},
                 # recipients are destinations: never editable here (re-propose instead)
                 "gmail.draft.create": {"subject", "body"}, "outlook.draft.create": {"subject", "body"}}


@router.post("/proposals/{pid}/revise")
def revise(pid: str, body: ReviseIn, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        pv = P.current_version(conn, me.user_id, pid)
        specs = P.specs_from_actions(P.actions_for_version(conn, pv["pv"]["id"]))
        remove = {i.index for i in body.items if i.remove}
        for it in body.items:
            if it.remove or not it.args:
                continue
            if not 0 <= it.index < len(specs):
                raise HTTPException(400, "bad item index")
            sp = specs[it.index]
            allowed = EDITABLE_ARGS.get(sp.action_type, set())
            bad = set(it.args) - allowed
            if bad:
                raise HTTPException(400, f"Fields not editable here: {sorted(bad)}")  # destinations/accounts can't be changed by edit
            if sp.action_type == "task.create" and ({"due_date", "due_datetime"} & set(it.args)):
                for k in ("due_date", "due_datetime", "due_local", "due_string"):
                    sp.args.pop(k, None)
            if sp.action_type == "file.write" and "content" in it.args:
                from jarvis.localops import files as F
                plan = F.plan_write(conn, me.user_id, sp.args["root_id"], it.args.get("path", sp.args["path"]), it.args["content"],
                                    allow_overwrite=sp.args.get("mode") == "overwrite")
                sp.args, sp.before, sp.after = plan["args"], plan["before"], plan["after"] | {"diff": plan["diff"]}
                continue
            sp.args.update({k: v for k, v in it.args.items() if v is not None})
            if sp.action_type == "task.create":
                sp.after = {"content": sp.args.get("content"), "due": sp.args.get("due_datetime") or sp.args.get("due_date")}
                sp.expected_effect = f"Create Todoist task “{sp.args.get('content')}”" + (
                    f" due {sp.args.get('due_datetime') or sp.args.get('due_date')}" if sp.args.get("due_datetime") or
                    sp.args.get("due_date") else "") + (f" in {sp.target_name}" if sp.target_name else "")
            if sp.action_type.endswith(".draft.create"):
                sp.after = (sp.after or {}) | {k: sp.args[k] for k in ("subject", "body")}
                label = "Gmail" if sp.action_type.startswith("gmail") else "Outlook"
                sp.expected_effect = f"Save a {label} draft to {', '.join(sp.args['to'])}: “{sp.args['subject']}” (not sent)"
            if sp.action_type == "event.create":
                sp.expected_effect = f"Add “{sp.args.get('summary')}” {sp.args['start'].get('dateTime')} to {sp.target_name}"
                sp.preconditions["slot_free_at"] = None   # edited time: freshness re-checked at execution
        specs = [s for i, s in enumerate(specs) if i not in remove]
        if not specs:
            P.reject(conn, me.user_id, pid)
            return P.view(conn, me.user_id, pid)
        P.revise(conn, me.user_id, pid, specs, "edited in Action Center")
    except P.ProposalError as e:
        _perr(e)
    return P.view(conn, me.user_id, pid)


class RetryIn(BaseModel):
    items: list[int]


@router.post("/proposals/{pid}/retry")
def retry(pid: str, body: RetryIn, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        P.retry(conn, me.user_id, pid, body.items)
    except P.ProposalError as e:
        _perr(e)
    return P.view(conn, me.user_id, pid)


@router.get("/actions/history")
def action_history(limit: int = 50, me: Principal = Depends(principal), conn=Depends(db)):
    acts = rows(conn, "SELECT a.*, v.proposal_id, v.version, p.title, p.capture_id FROM proposed_actions a "
                      "JOIN proposal_versions v ON v.id=a.proposal_version_id JOIN proposals p ON p.id=v.proposal_id "
                      "WHERE p.user_id=? AND a.state NOT IN ('proposed','superseded','skipped') ORDER BY a.updated_at DESC "
                      "LIMIT ?", (me.user_id, min(limit, 200)))
    out = []
    for a in acts:
        appr = row(conn, "SELECT id, channel, created_at, evidence FROM approvals WHERE proposal_version_id=? ORDER BY created_at "
                         "DESC LIMIT 1", (a["proposal_version_id"],))
        rc = row(conn, "SELECT * FROM verification_receipts WHERE action_id=? ORDER BY verified_at DESC LIMIT 1", (a["id"],))
        asked = row(conn, "SELECT text FROM transcript_versions WHERE capture_id=? ORDER BY version DESC LIMIT 1",
                    (a["capture_id"],)) if a["capture_id"] else None
        attempts = rows(conn, "SELECT attempt_no, started_at, finished_at, outcome, error FROM execution_attempts WHERE action_id=? "
                              "ORDER BY attempt_no", (a["id"],))
        acct = row(conn, "SELECT label, provider FROM integration_accounts WHERE id=?", (a["account_id"],)) if a["account_id"] else None
        undo = {"task.create": "Propose deleting or completing this task", "event.create": "Propose deleting this event",
                "task.complete": "Propose reopening the task", "file.write": "Restore from the saved backup via a new proposal",
                "file.move": "Propose moving it back"}.get(a["action_type"], "Propose a compensating change")
        args = jload(a["args"], {})
        args.pop("_target_name", None)
        out.append({"action_id": a["id"], "proposal_id": a["proposal_id"], "proposal_title": a["title"], "version": a["version"],
                    "asked": asked["text"] if asked else None, "action_type": a["action_type"], "args": args,
                    "expected_effect": a["expected_effect"], "state": a["state"], "account": acct,
                    "target_resource": a["target_resource"], "external_id": a["external_id"], "external_url": a["external_url"],
                    "approved": appr, "receipt": ({k: rc[k] for k in ("verified_at", "method", "ok", "simulated")} |
                                                  {"observed": jload(rc["observed"], {})}) if rc else None,
                    "attempts": attempts, "undo": undo, "last_error": a["last_error"],
                    # aliases used by the UI
                    "id": a["id"], "provider": a["provider"], "account_label": acct["label"] if acct else None,
                    "target": {"id": a["target_resource"], "name": jload(a["args"], {}).get("_target_name")},
                    "request_text": asked["text"] if asked else None, "approved_at": appr["created_at"] if appr else None,
                    "undoable": a["state"] == "verified",
                    "undo_until": a["undo_until"], "undone_by": a["undone_by"],
                    "auto_approved": bool(appr and appr["channel"] == "policy")})
    return out


class PauseIn(BaseModel):
    reason: str | None = None


@common.get("/control")
def control(me: Principal = Depends(principal), conn=Depends(db)):
    return executor.control_view(conn, me.user_id)


@router.post("/control/pause")
def pause(body: PauseIn, me: Principal = Depends(require("org.pause")), conn=Depends(db)):
    """Org-wide kill switch: holds every user's queued writes (admins)."""
    return executor.pause(conn, body.reason, by=me.user_id)


@router.post("/control/resume")
def resume(me: Principal = Depends(require("org.pause")), conn=Depends(db)):
    return executor.resume(conn, by=me.user_id)


# ================================================================ today / plan / briefings / schedules / notifications
@router.get("/today")
def today(refresh: bool = False, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.agents import visuals as V
    snap = ctx.today(conn, me.user_id, force=refresh)
    snap["calendar_visual"] = V.calendar(conn, me.user_id, date.fromisoformat(snap["date"]), 1, snap["tz"])
    snap["greeting_name"] = me.display_name
    return snap


@router.get("/weather")
def weather_now(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.agents import visuals as V
    from jarvis.agents import weather as W
    if not W.enabled(conn, me.user_id):
        return {"type": "weather", "disabled": True}
    loc = W.location(conn, me.user_id)
    try:
        data = W.fetch(loc, ctx.user_tz(conn, me.user_id))
    except W.WeatherUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return V.weather(data, loc, ctx.local_today(conn, me.user_id))


class PlanIn(BaseModel):
    mission: str = Field(min_length=1, max_length=300)
    priorities: list[str] = []
    constraints: list[str] = []
    first_action: str | None = None


@router.put("/plan/today")
def put_plan(body: PlanIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.briefings.workflows import save_plan
    uid = me.user_id
    save_plan(conn, uid, ctx.local_today(conn, uid), body.mission.strip(), [p for p in body.priorities if p.strip()][:3],
              body.constraints, body.first_action, author="user", status="accepted", reason="Set by the user")
    audit(conn, "user", "plan.accepted", f"Mission: {body.mission[:100]}", uid=uid)
    return ctx.current_plan(conn, uid, ctx.local_today(conn, uid))


@router.get("/plan/history")
def plan_history(d: str | None = None, me: Principal = Depends(principal), conn=Depends(db)):
    day = d or ctx.local_today(conn, me.user_id).isoformat()
    out = rows(conn, "SELECT v.* FROM daily_plan_versions v JOIN daily_plans p ON p.id=v.plan_id WHERE p.user_id=? "
                     "AND p.plan_date=? ORDER BY v.version DESC", (me.user_id, day))
    for v in out:
        v["priorities"], v["constraints"] = jload(v["priorities"], []), jload(v["constraints"], [])
    return out


@router.get("/briefings")
def briefings(kind: str | None = None, limit: int = 20, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.briefings.workflows import _view
    q = "SELECT * FROM briefings WHERE user_id=?" + (" AND kind=?" if kind else "") + " ORDER BY created_at DESC LIMIT ?"  # nosec B608 fixed clause; values bound
    return [_view(b) for b in rows(conn, q, (me.user_id,) + ((kind,) if kind else ()) + (min(limit, 100),))]


class BriefIn(BaseModel):
    kind: str
    force: bool = False


@router.post("/briefings/generate")
def gen_briefing(body: BriefIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.briefings import workflows
    if body.kind not in workflows.BUILDERS:
        raise HTTPException(400, "unknown kind")
    return workflows.generate(conn, me.user_id, body.kind, force=body.force)


class NightlyIn(BaseModel):
    briefing_id: str | None = None
    answers: dict[str, Any]


@router.post("/debriefs/nightly")
def nightly(body: NightlyIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.briefings.workflows import record_nightly_answers
    try:
        return record_nightly_answers(conn, me.user_id, body.briefing_id, body.answers)
    except KeyError as e:
        raise HTTPException(404, "Not found.") from e


@router.get("/schedules")
def schedules(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.briefings.scheduler import view
    return [view(conn, me.user_id, r["id"]) for r in rows(conn, "SELECT id FROM schedules WHERE user_id=? ORDER BY hour, minute",
                                                          (me.user_id,))]


class ScheduleIn(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(None, ge=0, le=23)
    minute: int | None = Field(None, ge=0, le=59)
    weekdays: list[int] | None = None
    destination: str | None = None


@router.patch("/schedules/{sid}")
def patch_schedule(sid: str, body: ScheduleIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.briefings.scheduler import update_schedule
    if body.destination and body.destination not in ("inbox", "inbox+push"):
        raise HTTPException(400, "bad destination")
    try:
        return update_schedule(conn, me.user_id, sid, body.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(404) from e


@common.get("/worker/status")
def worker_status(conn=Depends(db)):
    from jarvis.worker import status
    return status(conn)


@router.get("/notifications")
def notifications(me: Principal = Depends(principal), conn=Depends(db)):
    return rows(conn, "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (me.user_id,))


@router.post("/notifications/{nid}/read")
def notif_read(nid: str, me: Principal = Depends(principal), conn=Depends(db)):
    if not conn.execute("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=? AND user_id=?",
                        (now_iso(), nid, me.user_id)).rowcount:
        raise HTTPException(404, "Not found.")
    return {"ok": True}


@router.post("/notifications/{nid}/opened")
def notif_opened(nid: str, me: Principal = Depends(principal), conn=Depends(db)):
    if not conn.execute("UPDATE notifications SET opened_at=COALESCE(opened_at, ?), read_at=COALESCE(read_at, ?) "
                        "WHERE id=? AND user_id=?", (now_iso(), now_iso(), nid, me.user_id)).rowcount:
        raise HTTPException(404, "Not found.")
    return {"ok": True}


@router.get("/push/config")
def push_cfg():
    from jarvis.briefings.notify import push_config
    return push_config()


class PushSubIn(BaseModel):
    endpoint: str
    keys: dict[str, str] = {}


@router.post("/push/subscribe")
def push_sub(body: PushSubIn, request: Request, me: Principal = Depends(principal), conn=Depends(db)):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "Push endpoint must be https")
    # A device endpoint belongs to whoever subscribed it last (a shared browser switching users moves with them).
    conn.execute("INSERT INTO push_subscriptions (id, user_id, endpoint, keys, user_agent, created_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(endpoint) DO UPDATE SET keys=excluded.keys, user_id=excluded.user_id, revoked_at=NULL",
                 (new_id("psub"), me.user_id, body.endpoint, jdump(body.keys), request.headers.get("user-agent", "")[:200],
                  now_iso()))
    return {"ok": True}


@router.delete("/push/subscribe")
def push_unsub(body: PushSubIn, me: Principal = Depends(principal), conn=Depends(db)):
    conn.execute("UPDATE push_subscriptions SET revoked_at=? WHERE endpoint=? AND user_id=?",
                 (now_iso(), body.endpoint, me.user_id))
    return {"ok": True}


# ================================================================ memory / projects / knowledge
@router.get("/memories")
def memories(q: str | None = None, status: str | None = None, project_id: str | None = None, me: Principal = Depends(principal),
             conn=Depends(db)):
    from jarvis.memory import service as M
    return M.list_(conn, me.user_id, q=q, status=status, project_id=project_id)


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    type: str = "preference"
    project_id: str | None = None
    pinned: bool = False
    entity: str | None = None


@router.post("/memories")
def add_memory(body: MemoryIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.memory import service as M
    if body.project_id and not row(conn, "SELECT id FROM projects WHERE id=? AND user_id=?", (body.project_id, me.user_id)):
        raise HTTPException(404, "Not found.")
    with tx(conn):
        r = M.add(conn, me.user_id, body.content, body.type, "user_edit", project_id=body.project_id, pinned=body.pinned,
                  entity=body.entity)
    return M.get(conn, me.user_id, r["id"]) | {"superseded": r["superseded"], "conflicts": r["conflicts"]}


class MemoryPatch(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@router.patch("/memories/{mid}")
def patch_memory(mid: str, body: MemoryPatch, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.memory import service as M
    try:
        return M.correct(conn, me.user_id, mid, body.content)
    except KeyError as e:
        raise HTTPException(404) from e


@router.post("/memories/{mid}/forget")
def forget_memory(mid: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.memory import service as M
    try:
        M.forget(conn, me.user_id, mid)
    except KeyError as e:
        raise HTTPException(404, "Not found.") from e
    return {"ok": True, "note": "Removed from search and scrubbed locally. Existing backups may still contain it until deleted."}


class PinIn(BaseModel):
    pinned: bool


@router.post("/memories/{mid}/pin")
def pin_memory(mid: str, body: PinIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.memory import service as M
    if not M.set_pinned(conn, me.user_id, mid, body.pinned):
        raise HTTPException(404, "Not found.")
    return M.get(conn, me.user_id, mid)


@router.get("/memories/export")
def export_memories(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.memory import service as M
    return PlainTextResponse(M.export_markdown(conn, me.user_id), media_type="text/markdown",
                             headers={"Content-Disposition": "attachment; filename=jarvis-memory.md"})


@router.get("/projects")
def projects(me: Principal = Depends(principal), conn=Depends(db)):
    out = rows(conn, "SELECT * FROM projects WHERE user_id=? ORDER BY name", (me.user_id,))
    snap = ctx.tasks_snapshot(conn, me.user_id)
    from jarvis.agents.interpret import detect_area
    for p in out:
        p["memories_count"] = row(conn, "SELECT COUNT(*) c FROM memories WHERE project_id=? AND status='active'", (p["id"],))["c"]
        p["next_actions"] = [t["content"] for t in snap["tasks"] if detect_area(t["content"]) == p["area"]][:5]
        p["tasks_source"] = snap["status"]
        p["sensitive"] = bool(p["sensitive"])
    return out


class ProjectPatch(BaseModel):
    description: str | None = None
    notion_url: str | None = None
    visibility: str | None = Field(None, pattern="^(private|org)$")


@router.patch("/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch, me: Principal = Depends(principal), conn=Depends(db)):
    if body.notion_url and not body.notion_url.startswith("https://"):
        raise HTTPException(400, "Notion link must be https://")
    if not row(conn, "SELECT id FROM projects WHERE id=? AND user_id=?", (pid, me.user_id)):
        raise HTTPException(404, "Not found.")
    fields = body.model_dump(exclude_none=True)
    if fields:
        conn.execute(f"UPDATE projects SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=? AND user_id=?",  # nosec B608 columns = ProjectPatch field names; values bound
                     (*fields.values(), now_iso(), pid, me.user_id))
    return row(conn, "SELECT * FROM projects WHERE id=?", (pid,))


@router.post("/documents")
async def upload_document(file: UploadFile = File(...), project_id: str | None = Form(None), me: Principal = Depends(principal),
                          conn=Depends(db)):
    import anyio

    from jarvis.knowledge.ingest import ingest_bytes
    data = await file.read(20 * 1024 * 1024 + 1)
    try:
        res = await anyio.to_thread.run_sync(lambda: ingest_bytes(conn, me.user_id, data, file.filename or "upload",
                                                                  file.content_type, project_id or None))
    except KeyError as e:
        raise HTTPException(404, "Not found.") from e
    return res


@router.get("/documents")
def documents(me: Principal = Depends(principal), conn=Depends(db)):
    """Your documents plus documents others shared with the organization (marked `own: false`, read-only)."""
    out = rows(conn, "SELECT d.id, d.title, d.mime, d.origin, d.project_id, d.created_at, d.visibility, d.user_id = ? AS own, "
                     "v.version, v.sha256, v.status, v.processing, v.bytes, "
                     "(SELECT COUNT(*) FROM evidence_segments s WHERE s.source_version_id=v.id) segments "
                     "FROM source_documents d LEFT JOIN source_versions v ON v.id=d.current_version_id "
                     "WHERE d.deleted_at IS NULL AND (d.user_id=? OR d.visibility='org') ORDER BY d.created_at DESC",
               (me.user_id, me.user_id))
    for d in out:
        d["processing"] = jload(d["processing"], {})
        d["own"] = bool(d["own"])
    return out


class VisibilityIn(BaseModel):
    visibility: str = Field(pattern="^(private|org)$")


@router.post("/documents/{doc_id}/visibility")
def document_visibility(doc_id: str, body: VisibilityIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.knowledge.ingest import set_visibility
    try:
        with tx(conn):
            return set_visibility(conn, me.user_id, doc_id, body.visibility)
    except KeyError as e:
        raise HTTPException(404, "Not found.") from e


@router.get("/documents/{doc_id}")
def document(doc_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.knowledge.ingest import readable_document
    d = readable_document(conn, me.user_id, doc_id)
    if not d or d["deleted_at"]:
        raise HTTPException(404)
    v = row(conn, "SELECT * FROM source_versions WHERE id=?", (d["current_version_id"],))
    segs = rows(conn, "SELECT id, locator, kind, substr(text,1,300) excerpt FROM evidence_segments WHERE source_version_id=? "
                      "ORDER BY rowid", (v["id"],)) if v else []
    return d | {"version": v["version"] if v else None, "status": v["status"] if v else None,
                "processing": jload(v["processing"], {}) if v else {}, "segments": segs}


@router.delete("/documents/{doc_id}")
def delete_doc(doc_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.knowledge.ingest import delete_document
    res = delete_document(conn, me.user_id, doc_id)
    if not res.get("deleted"):
        raise HTTPException(404, "Not found.")
    return res | {"note": "Backups may retain it until deleted."}


@router.get("/search")
def search(q: str, project_id: str | None = None, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.knowledge.search import search as s
    return s(conn, me.user_id, q, project_id=project_id, limit=20, allowed_project_ids={project_id} if project_id else None)


class EvidenceIn(BaseModel):
    document_ids: list[str]
    question: str = ""


@router.post("/evidence/preview")
def evidence_preview(body: EvidenceIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.knowledge.vision import evidence_preview as ep
    return ep(conn, me.user_id, body.document_ids, body.question) | {"leaves_machine": True,
                                                          "destination": "Anthropic API (only when you ask)"}


# ================================================================ integrations
def _serr(e):
    raise HTTPException(400, {"code": e.code, "message": e.message})


@router.get("/integrations")
def integrations(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import list_integrations
    return list_integrations(conn, me.user_id)


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)


@router.post("/integrations/todoist")
def todoist_connect(body: TokenIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_todoist
    try:
        return connect_todoist(conn, me.user_id, body.token)
    except SetupError as e:
        _serr(e)


@router.get("/integrations/todoist/projects")
def todoist_projects(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import accounts as A
    from jarvis.integrations.service import todoist_projects as tp
    try:
        return tp(conn, me.user_id)
    except A.NotConnected as e:
        raise HTTPException(400, str(e)) from e


class TargetIn(BaseModel):
    project_id: str
    area_projects: dict[str, str] | None = None


@router.post("/integrations/todoist/target")
def todoist_target(body: TargetIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_todoist_target
    try:
        return set_todoist_target(conn, me.user_id, body.project_id, body.area_projects)
    except SetupError as e:
        _serr(e)


@router.post("/integrations/todoist/sync")
def todoist_sync(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.base import ProviderError
    from jarvis.integrations.service import todoist_sync as ts
    try:
        return ts(conn, me.user_id)
    except ProviderError as e:
        raise HTTPException(502, {"code": e.kind, "message": e.message}) from e


@router.get("/integrations/google/setup")
def google_setup():
    from jarvis.integrations.service import google_setup_info
    return google_setup_info()


class PurposeIn(BaseModel):
    purpose: str


@router.post("/integrations/google/start")
def google_start(body: PurposeIn, request: Request, response: Response, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import google_start as gs
    try:
        url, binding = gs(conn, me.user_id, body.purpose, request.headers.get("origin"))
    except SetupError as e:
        _serr(e)
    # Lax (not Strict) so the browser sends it on the redirect back from Google; scoped to the callback path only.
    response.set_cookie(OAUTH_BINDING_COOKIE, binding, httponly=True, samesite="lax", secure=get_settings().remote_mode,
                        max_age=600, path=OAUTH_CALLBACK_PATH)
    return {"auth_url": url}


@router.get("/integrations/google/calendars")
def google_cals(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import accounts as A
    from jarvis.integrations.service import google_calendars
    try:
        return google_calendars(conn, me.user_id)
    except A.NotConnected as e:
        raise HTTPException(400, str(e)) from e


class CalSelIn(BaseModel):
    read: list[str]
    write_target: str | None = None


@router.post("/integrations/google/calendars")
def google_cals_set(body: CalSelIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_calendars
    try:
        return set_calendars(conn, me.user_id, body.read, body.write_target)
    except SetupError as e:
        _serr(e)


class IdsIn(BaseModel):
    ids: list[str]


@router.post("/integrations/drive/roots")
def drive_roots(body: IdsIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_drive_roots
    try:
        return set_drive_roots(conn, me.user_id, body.ids)
    except SetupError as e:
        _serr(e)


@router.post("/integrations/drive/ingest/{file_id}")
def drive_ingest(file_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import drive_ingest as di
    try:
        return di(conn, me.user_id, file_id)
    except SetupError as e:
        _serr(e)


class UrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@router.post("/integrations/ics")
def ics_connect(body: UrlIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_ics
    try:
        return connect_ics(conn, me.user_id, body.url)
    except SetupError as e:
        _serr(e)


@router.post("/integrations/ics/refresh")
def ics_refresh(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import ics_refresh as r
    try:
        return r(conn, me.user_id)
    except SetupError as e:
        _serr(e)


@router.get("/deadlines")
def deadlines(me: Principal = Depends(principal), conn=Depends(db)):
    return rows(conn, "SELECT d.* FROM deadline_items d JOIN integration_accounts a ON a.id=d.account_id WHERE a.user_id=? "
                      "AND d.status!='cancelled' ORDER BY d.local_date, d.start_at LIMIT 300", (me.user_id,))


@router.get("/course-mappings")
def course_mappings(me: Principal = Depends(principal), conn=Depends(db)):
    return rows(conn, "SELECT * FROM course_mappings WHERE user_id=? ORDER BY pattern", (me.user_id,))


class MappingIn(BaseModel):
    pattern: str
    course: str
    role: str
    confirmed: bool = True


@router.post("/course-mappings")
def add_mapping(body: MappingIn, me: Principal = Depends(principal), conn=Depends(db)):
    if body.role not in ("own", "gta"):
        raise HTTPException(400, "role must be own or gta")
    conn.execute("INSERT INTO course_mappings (id, user_id, pattern, course, role, confirmed, created_at) VALUES (?,?,?,?,?,?,?) "
                 "ON CONFLICT(user_id, pattern) DO UPDATE SET course=excluded.course, role=excluded.role, "
                 "confirmed=excluded.confirmed",
                 (new_id("cm"), me.user_id, body.pattern.strip(), body.course.strip(), body.role, int(body.confirmed), now_iso()))
    return rows(conn, "SELECT * FROM course_mappings WHERE user_id=? ORDER BY pattern", (me.user_id,))


class GitHubIn(BaseModel):
    token: str
    repos: list[str]


@router.post("/integrations/github")
def github_connect(body: GitHubIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_github
    try:
        return connect_github(conn, me.user_id, body.token, body.repos)
    except SetupError as e:
        _serr(e)


@router.get("/github/status")
def github_status(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import github_status as gs
    return gs(conn, me.user_id)


class KeyIn(BaseModel):
    api_key: str


@router.post("/integrations/anthropic")
def anthropic_connect(body: KeyIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    """The org's model key (one key pays for the install): admins only."""
    from jarvis.integrations.service import SetupError, connect_anthropic
    try:
        return connect_anthropic(conn, body.api_key)
    except SetupError as e:
        _serr(e)


@router.delete("/integrations/{account_id}")
def disconnect(account_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import accounts as A
    a = A.get_account_by_id(conn, account_id)
    # Your own accounts, or (admins) the org-level ones. Anything else is "not found".
    if not a or not (a["user_id"] == me.user_id or (a["user_id"] is None and me.can("org.integrations"))):
        raise HTTPException(404)
    if a["provider"] in ("google", "gmail") and a["secret_ciphertext"]:
        import json

        from jarvis.integrations.google_oauth import revoke
        try:
            revoke(json.loads(A.read_secret(a)).get("refresh_token") or "")
        except Exception:
            pass
    A.disconnect(conn, account_id, by=me.user_id)
    return {"ok": True}


# ================================================================ local operator
class RootIn(BaseModel):
    path: str
    label: str
    writable: bool = False
    user_id: str | None = None        # who the folder is for (default: the admin adding it)


@router.get("/localops/roots")
def roots(all: bool = False, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.localops.files import list_all_roots, list_roots
    if all and me.can("org.integrations"):
        return list_all_roots(conn)
    return list_roots(conn, me.user_id)


@router.post("/localops/roots")
def add_root(body: RootIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    """Folders on the JARVIS host are designated by admins and assigned to one user."""
    from jarvis.localops.files import FileOpError
    from jarvis.localops.files import add_root as ar
    owner = body.user_id or me.user_id
    if not row(conn, "SELECT id FROM users WHERE id=? AND status='active'", (owner,)):
        raise HTTPException(404, "No such user.")
    try:
        return ar(conn, owner, body.path, body.label, body.writable, by=me.user_id)
    except FileOpError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message}) from e


@router.delete("/localops/roots/{rid}")
def del_root(rid: str, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.knowledge import vault
    from jarvis.localops.files import remove_root
    r = row(conn, "SELECT user_id FROM scope_grants WHERE id=? AND resource_type='local_root'", (rid,))
    if not r:
        raise HTTPException(404, "Not found.")
    vault.forget_root(conn, r["user_id"], rid)        # a removed root's notes must not stay searchable
    remove_root(conn, rid)
    audit(conn, "user", "localops.root_removed", "Workspace root removed", "scope_grant", rid, uid=me.user_id)
    return {"ok": True}


def _fop(fn, *a):
    from jarvis.localops.files import FileOpError
    try:
        return fn(*a)
    except FileOpError as e:
        if e.code == "no_root":       # unknown or someone else's root
            raise HTTPException(404, "Not found.") from e
        raise HTTPException(400, {"code": e.code, "message": e.message}) from e


@router.get("/localops/list")
def lo_list(root_id: str, path: str = "", me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.localops import files as F
    return _fop(lambda: F.list_dir(F.get_root(conn, me.user_id, root_id), path))


@router.get("/localops/read")
def lo_read(root_id: str, path: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.localops import files as F
    return _fop(lambda: F.read_file(F.get_root(conn, me.user_id, root_id), path))


@router.get("/localops/search")
def lo_search(root_id: str, q: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.localops import files as F
    return _fop(lambda: F.search(F.get_root(conn, me.user_id, root_id), q))


# ================================================================ settings / budget / diagnostics / backups
PERSONAL_KEYS = ("voice", "privacy", "notifications", "research", "location", "address_as", "weather", "sandbox")
ORG_KEYS = ("models", "brand")


@router.get("/settings")
def get_settings_(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.core import settings as S
    data = S.user_data(conn, me.user_id)
    org = S.org_data(conn)
    ur = S.user_row(conn, me.user_id)
    return {"display_name": me.display_name, "timezone": ur["timezone"], "version": ur["version"],
            **{k: data[k] for k in data if k not in ("budget",)},
            # org-wide (read-only unless you're an admin)
            "models": org.get("models", {}), "brand": org.get("brand", {}), "can_edit_org": me.can("org.settings"),
            "data_flow": ["Audio → local faster-whisper on this machine (never uploaded).",
                          "Typed/spoken commands are interpreted locally; only when the model is enabled are selected "
                          "prompts, excerpts and images sent to Anthropic.",
                          "Todoist/Google/GitHub calls go directly from this machine to those providers.",
                          "No telemetry or third-party tracking."]}


class SettingsPatch(BaseModel):
    display_name: str | None = Field(None, max_length=80)
    timezone: str | None = None
    voice: dict[str, Any] | None = None
    privacy: dict[str, Any] | None = None
    notifications: dict[str, Any] | None = None
    research: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    address_as: dict[str, Any] | None = None
    weather: dict[str, Any] | None = None
    sandbox: dict[str, Any] | None = None
    models: dict[str, str] | None = None        # org-wide: admins only
    brand: dict[str, str] | None = None         # org-wide: admins only


@router.get("/brain")
def brain(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.agents import agent, tools
    from jarvis.core import settings as S
    ok, why = agent.available(conn)
    data = S.user_data(conn, me.user_id)
    return {"mode": "v2_agent" if ok else "offline", "reason": None if ok else why,
            "web_research": data.get("research", {}).get("web_enabled", True),
            "max_searches": agent.search_limit(conn, me.user_id),
            "fast_mode": bool(data.get("research", {}).get("fast_mode")),
            "effort": data.get("research", {}).get("effort", "auto"),
            "model": (S.org_data(conn).get("models") or {}).get("agent") or get_settings().model_default,
            "tools": [t.name for t in tools.offered(conn, me.user_id)],
            "assistant_name": agent.assistant_name(conn),
            "notes": "Changes to Todoist/Calendar/files are always proposals you approve. Web search costs ~$0.01 per search "
                     "plus model tokens, all counted against the budget."}


@router.patch("/settings")
def patch_settings(body: SettingsPatch, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.core import settings as S
    if (body.models is not None or body.brand is not None) and not me.can("org.settings"):
        raise HTTPException(403, "Models and branding are organization settings; ask an admin.")
    if body.timezone:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(body.timezone)
        except Exception as e:
            raise HTTPException(400, "bad timezone") from e
    with tx(conn):
        def upd(data: dict) -> None:
            for k in PERSONAL_KEYS:
                v = getattr(body, k)
                if v is not None:
                    data[k] = (data.get(k) or {}) | v
        S.update_user_data(conn, me.user_id, upd)
        if body.timezone:
            conn.execute("UPDATE user_settings SET timezone=? WHERE user_id=?", (body.timezone, me.user_id))
        if body.display_name and body.display_name.strip():
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), me.user_id))
        if body.models is not None or body.brand is not None:
            def org_upd(data: dict) -> None:
                for k in ORG_KEYS:
                    v = getattr(body, k)
                    if v is not None:
                        data[k] = (data.get(k) or {}) | v
            S.update_org_data(conn, org_upd)
            audit(conn, "user", "org.settings_changed", "Organization models/branding changed", uid=me.user_id)
    return get_settings_(me=from_session_refresh(conn, me), conn=conn)


def from_session_refresh(conn, me: Principal) -> Principal:
    """The principal with a freshly read display name (after a rename in the same request)."""
    r = row(conn, "SELECT display_name FROM users WHERE id=?", (me.user_id,))
    return Principal(me.user_id, me.username, r["display_name"] if r else me.display_name, me.role, me.session_id,
                     me.auth_level)


@router.get("/budget")
def budget(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.llm import budget as B
    return B.summary(conn, me.user_id) | {"can_edit": me.can("org.settings")}


class BudgetIn(BaseModel):
    paid_inference_enabled: bool | None = None
    background_paid_enabled: bool | None = None
    daily_limit_usd: float | None = Field(None, ge=0)
    monthly_limit_usd: float | None = Field(None, ge=0)
    user_daily_limit_usd: float | None = Field(None, ge=0)
    user_monthly_limit_usd: float | None = Field(None, ge=0)
    max_output_tokens: int | None = Field(None, ge=100, le=64000)
    max_tool_calls: int | None = Field(None, ge=0, le=20)
    max_specialists: int | None = Field(None, ge=1, le=5)
    effort: str | None = None


@router.patch("/budget")
def patch_budget(body: BudgetIn, me: Principal = Depends(require("org.settings")), conn=Depends(db)):
    from jarvis.llm import budget as B
    patch = body.model_dump(exclude_unset=True)
    if patch.get("effort") and patch["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "bad effort")
    if patch.get("paid_inference_enabled") and patch.get("daily_limit_usd") is None and \
            B.budget_settings(conn).get("daily_limit_usd") is None:
        raise HTTPException(400, "Set a daily limit before enabling paid inference.")
    B.update_budget(conn, patch)
    audit(conn, "user", "budget.updated", f"Budget settings changed: {sorted(patch)}", uid=me.user_id)
    return B.summary(conn, me.user_id)


@router.get("/doctor")
def doctor(me: Principal = Depends(require("org.doctor")), conn=Depends(db)):
    from jarvis.doctor import checks
    return {"checks": checks(conn)}


@router.get("/metrics/latency")
def latency(me: Principal = Depends(require("org.doctor")), conn=Depends(db)):
    out = {}
    for k in [r["kind"] for r in rows(conn, "SELECT DISTINCT kind FROM latency_metrics")]:
        ms = [r["ms"] for r in rows(conn, "SELECT ms FROM latency_metrics WHERE kind=? ORDER BY id DESC LIMIT 200", (k,))]
        srt = sorted(ms)
        out[k] = {"count": len(ms), "p50_ms": srt[len(srt) // 2], "p95_ms": srt[min(len(srt) - 1, int(len(srt) * 0.95))],
                  "last_ms": ms[0]}
    return out


@common.get("/activity")
def activity(limit: int = 100, actor: str | None = None, scope: str = "mine", me: Principal = Depends(principal),
             conn=Depends(db)):
    """Your own activity. With scope=org (admins/auditors): the whole org's audit trail — who did what and when — with
    other people's summaries reduced to the event kind (summaries can contain content, e.g. memory text)."""
    lim = min(limit, 500)
    org = scope == "org" and me.can("audit.read")
    ev = [] if (actor or org) else rows(conn, "SELECT e.created_at at, e.kind, e.message, e.run_id FROM run_events e "
                                             "JOIN runs r ON r.id=e.run_id WHERE r.user_id=? ORDER BY e.id DESC LIMIT ?",
                                       (me.user_id, lim))
    where, params = [], []
    if not org:
        where.append("a.actor_user_id=?")
        params.append(me.user_id)
    if actor:
        where.append("a.actor=?")
        params.append(actor)
    au = rows(conn, "SELECT a.at, a.actor, a.kind, a.summary message, a.entity_type, a.entity_id, a.actor_user_id, "  # nosec B608 fixed clauses; values bound
                    "u.username FROM audit_events a LEFT JOIN users u ON u.id=a.actor_user_id "
                    + ("WHERE " + " AND ".join(where) + " " if where else "") + "ORDER BY a.id DESC LIMIT ?",
              (*params, lim))
    for e in au:
        if e["actor_user_id"] != me.user_id:
            e["message"] = e["kind"].replace("_", " ").replace(".", ": ")      # metadata only
            e["entity_id"] = None if e["entity_type"] not in ("user", "session", "mcp_server", "skill") else e["entity_id"]
    return sorted(ev + au, key=lambda e: e["at"], reverse=True)[:limit]


@router.post("/backups")
def make_backup(me: Principal = Depends(require("org.backups"))):
    from jarvis.backup import create_backup
    return create_backup()


@router.get("/backups")
def backups(me: Principal = Depends(require("org.backups"))):
    from jarvis.backup import list_backups
    return list_backups()


@router.post("/backups/{bid}/restore-preview")
def restore_preview(bid: str, me: Principal = Depends(require("org.backups"))):
    from jarvis.backup import restore_preview as rp
    try:
        return rp(bid)
    except FileNotFoundError as e:
        raise HTTPException(404) from e


class ConfirmIn(BaseModel):
    confirm: bool


@router.post("/backups/{bid}/restore")
def restore(bid: str, body: ConfirmIn, me: Principal = Depends(require("users.owner"))):
    """Restoring rolls back every user's data: owners only."""
    from jarvis.backup import restore as rs
    if not body.confirm:
        raise HTTPException(400, "Confirmation required.")
    try:
        return rs(bid)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


_ = date
