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
from jarvis.api.app import db, require_session
from jarvis.config import get_settings
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, new_id, now_iso, record_latency
from jarvis.security import auth

public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api")


def _cookie(resp: Response, token: str) -> None:
    s = get_settings()
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="strict", secure=s.remote_mode,
                    max_age=s.session_ttl_hours * 3600, path="/")


def _perr(e: P.ProposalError):
    raise HTTPException(e.status, {"code": e.code, "message": e.message})


# ================================================================ auth (public)
class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PairIn(BaseModel):
    code: str
    label: str | None = None


@public.get("/auth/status")
def auth_status(request: Request, conn=Depends(db)):
    s = auth.session_from_request(conn, request)
    exists = auth.owner_exists(conn)
    return {"owner_exists": exists, "authenticated": bool(s), "csrf_token": s["csrf_token"] if s else None,
            "remote_mode": get_settings().remote_mode, "setup_allowed": (not exists) and auth.is_loopback(request)}


@public.post("/auth/setup")
def auth_setup(body: PasswordIn, request: Request, response: Response, conn=Depends(db)):
    if not auth.is_loopback(request):
        raise HTTPException(403, "First-run setup is only allowed from the machine running Jarvis.")
    with tx(conn):
        auth.create_owner(conn, body.password)
        _, token, csrf = auth.create_session(conn, "setup")
    _cookie(response, token)
    return {"csrf_token": csrf}


_login_attempts: list[float] = []


@public.post("/auth/login")
def auth_login(body: PasswordIn, response: Response, conn=Depends(db)):
    now = time.monotonic()
    _login_attempts[:] = [t for t in _login_attempts if now - t < 300]
    if len(_login_attempts) >= 10:
        raise HTTPException(429, "Too many attempts; wait a few minutes.")
    _login_attempts.append(now)
    with tx(conn):
        _, token, csrf = auth.login(conn, body.password)
    _cookie(response, token)
    return {"csrf_token": csrf}


@public.post("/auth/pair")
def auth_pair(body: PairIn, response: Response, conn=Depends(db)):
    with tx(conn):
        _, token, csrf = auth.redeem_pairing_code(conn, body.code, body.label)
    _cookie(response, token)
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
        resp = RedirectResponse("/settings?connected=google", status_code=303)
    except SetupError as e:
        resp_err = e.code
        resp = RedirectResponse(f"/settings?connect_error={resp_err}", status_code=303)
    resp.delete_cookie(OAUTH_BINDING_COOKIE, path=OAUTH_CALLBACK_PATH)
    return resp


OAUTH_BINDING_COOKIE = "jarvis_oauth_binding"
OAUTH_CALLBACK_PATH = "/api/integrations/google/callback"


# ================================================================ auth (session)
@router.post("/auth/logout")
def auth_logout(response: Response, session=Depends(require_session), conn=Depends(db)):
    conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now_iso(), session["id"]))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/pairing-code")
def pairing_code(conn=Depends(db)):
    code = auth.create_pairing_code(conn)
    return {"code": code, "expires_in_s": 600}


# ================================================================ conversations / voice / commands
@router.post("/conversations")
def new_conversation(conn=Depends(db)):
    return {"id": chief.ensure_conversation(conn, None)}


@router.get("/conversations/{cid}/messages")
def conversation_messages(cid: str, conn=Depends(db)):
    return rows(conn, "SELECT id, role, text, spoken_text, capture_id, proposal_id, run_id, created_at FROM messages "
                      "WHERE conversation_id=? ORDER BY created_at", (cid,))


@router.get("/conversations")
def conversations(conn=Depends(db)):
    return rows(conn, "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 30")


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


@router.post("/voice/tts")
def tts(body: TTSIn, conn=Depends(db)):

    from jarvis.voice.tts import TTSUnavailable, get_tts
    try:
        wav = get_tts().synthesize(body.text, body.preset)
    except TTSUnavailable as e:
        raise HTTPException(503, {"code": "tts_unavailable", "message": str(e)}) from e
    record_latency(conn, "tts", get_tts().last_ms or 0, {"preset": body.preset, "chars": len(body.text)})
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


class VoiceSessionIn(BaseModel):
    mode: str = "push_to_talk"
    conversation_id: str | None = None


@router.post("/voice/sessions")
def voice_session(body: VoiceSessionIn, conn=Depends(db)):
    if body.mode not in ("push_to_talk", "hands_free"):
        raise HTTPException(400, "bad mode")
    cid = chief.ensure_conversation(conn, body.conversation_id)
    vid = new_id("vs")
    max_s = 600 if body.mode == "hands_free" else 3600
    conn.execute("INSERT INTO voice_sessions (id, conversation_id, mode, started_at, max_duration_s) VALUES (?,?,?,?,?)",
                 (vid, cid, body.mode, now_iso(), max_s))
    return {"id": vid, "max_duration_s": max_s, "conversation_id": cid}


@router.post("/voice/sessions/{vid}/end")
def voice_session_end(vid: str, conn=Depends(db)):
    conn.execute("UPDATE voice_sessions SET status='ended', ended_at=? WHERE id=?", (now_iso(), vid))
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
                        retain_audio: str = Form("false"), conn=Depends(db)):
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
    cid = chief.ensure_conversation(conn, conversation_id)
    cap_id = new_id("cap")
    conn.execute("INSERT INTO captures (id, conversation_id, voice_session_id, kind, captured_at, capture_tz, status, audio_mime, "
                 "audio_bytes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (cap_id, cid, voice_session_id, "audio", captured_at, tz, "transcribing", file.content_type, len(data), now_iso()))
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
    retained = retain_audio.lower() == "true" and jload(row(conn, "SELECT data FROM user_settings WHERE id=1")["data"], {}) \
        .get("privacy", {}).get("retain_audio", False)
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
def edit_transcript(cap_id: str, body: TranscriptEdit, conn=Depends(db)):
    with tx(conn):
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


@router.post("/commands")
def command(body: CommandIn, session=Depends(require_session), conn=Depends(db)):
    if not body.capture_id and not body.text.strip():
        raise HTTPException(400, "Say or type something.")
    if body.channel not in ("voice", "typed"):
        raise HTTPException(400, "bad channel")
    inp = chief.CommandInput(conversation_id=body.conversation_id or "", text=body.text, channel=body.channel,
                             captured_at=body.captured_at or now_iso(), tz=body.tz, capture_id=body.capture_id,
                             transcript_version=body.transcript_version, speech_playing=body.speech_playing,
                             attachments=body.attachments, session_id=session["id"])
    try:
        return chief.handle(conn, inp)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, conn=Depends(db)):
    r = row(conn, "SELECT status FROM runs WHERE id=?", (run_id,))
    if not r:
        raise HTTPException(404)
    conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
    note = "Run already finished; to stop an approved plan use Reject or Pause actions." if r["status"] != "running" else \
        "Cancellation requested; no new tool calls will start."
    return {"ok": True, "status": r["status"], "note": note}


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, conn=Depends(db)):
    out = rows(conn, "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
    for e in out:
        e["data"] = jload(e["data"], {})
    return out


# ================================================================ proposals / control
@router.get("/proposals")
def list_proposals(status: str = "active", conn=Depends(db)):
    if status == "active":
        ids = rows(conn, "SELECT id FROM proposals WHERE status IN ('proposed','queued','executing','unknown','partial','failed') "
                         "ORDER BY updated_at DESC LIMIT 30")
    else:
        ids = rows(conn, "SELECT id FROM proposals ORDER BY updated_at DESC LIMIT 100")
    return [P.view(conn, r["id"]) for r in ids]


@router.get("/proposals/{pid}")
def get_proposal(pid: str, conn=Depends(db)):
    try:
        return P.view(conn, pid)
    except P.ProposalError as e:
        _perr(e)


class ApproveIn(BaseModel):
    version: int
    hash: str
    selected_items: list[int]


@router.post("/proposals/{pid}/approve")
def approve(pid: str, body: ApproveIn, session=Depends(require_session), conn=Depends(db)):
    try:
        res = P.approve(conn, pid, body.version, body.hash, body.selected_items, channel="button", session_id=session["id"])
    except P.ProposalError as e:
        _perr(e)
    return res | {"proposal": P.view(conn, pid)}


@router.post("/proposals/{pid}/reject")
def reject(pid: str, conn=Depends(db)):
    try:
        P.reject(conn, pid)
    except P.ProposalError as e:
        _perr(e)
    return P.view(conn, pid)


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
                 "file.write": {"path", "content"}}


@router.post("/proposals/{pid}/revise")
def revise(pid: str, body: ReviseIn, conn=Depends(db)):
    try:
        pv = P.current_version(conn, pid)
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
                plan = F.plan_write(conn, sp.args["root_id"], it.args.get("path", sp.args["path"]), it.args["content"],
                                    allow_overwrite=sp.args.get("mode") == "overwrite")
                sp.args, sp.before, sp.after = plan["args"], plan["before"], plan["after"] | {"diff": plan["diff"]}
                continue
            sp.args.update({k: v for k, v in it.args.items() if v is not None})
            if sp.action_type == "task.create":
                sp.after = {"content": sp.args.get("content"), "due": sp.args.get("due_datetime") or sp.args.get("due_date")}
                sp.expected_effect = f"Create Todoist task “{sp.args.get('content')}”" + (
                    f" due {sp.args.get('due_datetime') or sp.args.get('due_date')}" if sp.args.get("due_datetime") or
                    sp.args.get("due_date") else "") + (f" in {sp.target_name}" if sp.target_name else "")
            if sp.action_type == "event.create":
                sp.expected_effect = f"Add “{sp.args.get('summary')}” {sp.args['start'].get('dateTime')} to {sp.target_name}"
                sp.preconditions["slot_free_at"] = None   # edited time: freshness re-checked at execution
        specs = [s for i, s in enumerate(specs) if i not in remove]
        if not specs:
            P.reject(conn, pid)
            return P.view(conn, pid)
        P.revise(conn, pid, specs, "edited in Action Center")
    except P.ProposalError as e:
        _perr(e)
    return P.view(conn, pid)


class RetryIn(BaseModel):
    items: list[int]


@router.post("/proposals/{pid}/retry")
def retry(pid: str, body: RetryIn, conn=Depends(db)):
    try:
        P.retry(conn, pid, body.items)
    except P.ProposalError as e:
        _perr(e)
    return P.view(conn, pid)


@router.get("/actions/history")
def action_history(limit: int = 50, conn=Depends(db)):
    acts = rows(conn, "SELECT a.*, v.proposal_id, v.version, p.title, p.capture_id FROM proposed_actions a "
                      "JOIN proposal_versions v ON v.id=a.proposal_version_id JOIN proposals p ON p.id=v.proposal_id "
                      "WHERE a.state NOT IN ('proposed','superseded','skipped') ORDER BY a.updated_at DESC LIMIT ?", (min(limit, 200),))
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
                    "undoable": a["state"] == "verified"})
    return out


class PauseIn(BaseModel):
    reason: str | None = None


@router.get("/control")
def control(conn=Depends(db)):
    return executor.control_view(conn)


@router.post("/control/pause")
def pause(body: PauseIn, conn=Depends(db)):
    return executor.pause(conn, body.reason)


@router.post("/control/resume")
def resume(conn=Depends(db)):
    return executor.resume(conn)


# ================================================================ today / plan / briefings / schedules / notifications
@router.get("/today")
def today(refresh: bool = False, conn=Depends(db)):
    return ctx.today(conn, force=refresh)


class PlanIn(BaseModel):
    mission: str = Field(min_length=1, max_length=300)
    priorities: list[str] = []
    constraints: list[str] = []
    first_action: str | None = None


@router.put("/plan/today")
def put_plan(body: PlanIn, conn=Depends(db)):
    from jarvis.briefings.workflows import save_plan
    save_plan(conn, ctx.local_today(conn), body.mission.strip(), [p for p in body.priorities if p.strip()][:3], body.constraints,
              body.first_action, author="user", status="accepted", reason="Set by AJ")
    audit(conn, "user", "plan.accepted", f"Mission: {body.mission[:100]}")
    return ctx.current_plan(conn, ctx.local_today(conn))


@router.get("/plan/history")
def plan_history(d: str | None = None, conn=Depends(db)):
    day = d or ctx.local_today(conn).isoformat()
    out = rows(conn, "SELECT v.* FROM daily_plan_versions v JOIN daily_plans p ON p.id=v.plan_id WHERE p.plan_date=? "
                     "ORDER BY v.version DESC", (day,))
    for v in out:
        v["priorities"], v["constraints"] = jload(v["priorities"], []), jload(v["constraints"], [])
    return out


@router.get("/briefings")
def briefings(kind: str | None = None, limit: int = 20, conn=Depends(db)):
    from jarvis.briefings.workflows import _view
    q = "SELECT * FROM briefings" + (" WHERE kind=?" if kind else "") + " ORDER BY created_at DESC LIMIT ?"
    return [_view(b) for b in rows(conn, q, ((kind,) if kind else ()) + (min(limit, 100),))]


class BriefIn(BaseModel):
    kind: str
    force: bool = False


@router.post("/briefings/generate")
def gen_briefing(body: BriefIn, conn=Depends(db)):
    from jarvis.briefings import workflows
    if body.kind not in workflows.BUILDERS:
        raise HTTPException(400, "unknown kind")
    return workflows.generate(conn, body.kind, force=body.force)


class NightlyIn(BaseModel):
    briefing_id: str | None = None
    answers: dict[str, Any]


@router.post("/debriefs/nightly")
def nightly(body: NightlyIn, conn=Depends(db)):
    from jarvis.briefings.workflows import record_nightly_answers
    return record_nightly_answers(conn, body.briefing_id, body.answers)


@router.get("/schedules")
def schedules(conn=Depends(db)):
    from jarvis.briefings.scheduler import view
    return [view(conn, r["id"]) for r in rows(conn, "SELECT id FROM schedules ORDER BY hour, minute")]


class ScheduleIn(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(None, ge=0, le=23)
    minute: int | None = Field(None, ge=0, le=59)
    weekdays: list[int] | None = None
    destination: str | None = None


@router.patch("/schedules/{sid}")
def patch_schedule(sid: str, body: ScheduleIn, conn=Depends(db)):
    from jarvis.briefings.scheduler import update_schedule
    if body.destination and body.destination not in ("inbox", "inbox+push"):
        raise HTTPException(400, "bad destination")
    try:
        return update_schedule(conn, sid, body.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(404) from e


@router.get("/worker/status")
def worker_status(conn=Depends(db)):
    from jarvis.worker import status
    return status(conn)


@router.get("/notifications")
def notifications(conn=Depends(db)):
    return rows(conn, "SELECT * FROM notifications ORDER BY created_at DESC LIMIT 100")


@router.post("/notifications/{nid}/read")
def notif_read(nid: str, conn=Depends(db)):
    conn.execute("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=?", (now_iso(), nid))
    return {"ok": True}


@router.post("/notifications/{nid}/opened")
def notif_opened(nid: str, conn=Depends(db)):
    conn.execute("UPDATE notifications SET opened_at=COALESCE(opened_at, ?), read_at=COALESCE(read_at, ?) WHERE id=?",
                 (now_iso(), now_iso(), nid))
    return {"ok": True}


@router.get("/push/config")
def push_cfg():
    from jarvis.briefings.notify import push_config
    return push_config()


class PushSubIn(BaseModel):
    endpoint: str
    keys: dict[str, str] = {}


@router.post("/push/subscribe")
def push_sub(body: PushSubIn, request: Request, conn=Depends(db)):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "Push endpoint must be https")
    conn.execute("INSERT INTO push_subscriptions (id, endpoint, keys, user_agent, created_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(endpoint) DO UPDATE SET keys=excluded.keys, revoked_at=NULL",
                 (new_id("psub"), body.endpoint, jdump(body.keys), request.headers.get("user-agent", "")[:200], now_iso()))
    return {"ok": True}


@router.delete("/push/subscribe")
def push_unsub(body: PushSubIn, conn=Depends(db)):
    conn.execute("UPDATE push_subscriptions SET revoked_at=? WHERE endpoint=?", (now_iso(), body.endpoint))
    return {"ok": True}


# ================================================================ memory / projects / knowledge
@router.get("/memories")
def memories(q: str | None = None, status: str | None = None, project_id: str | None = None, conn=Depends(db)):
    from jarvis.memory import service as M
    return M.list_(conn, q=q, status=status, project_id=project_id)


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    type: str = "preference"
    project_id: str | None = None
    pinned: bool = False
    entity: str | None = None


@router.post("/memories")
def add_memory(body: MemoryIn, conn=Depends(db)):
    from jarvis.memory import service as M
    with tx(conn):
        r = M.add(conn, body.content, body.type, "user_edit", project_id=body.project_id, pinned=body.pinned, entity=body.entity)
    return M.get(conn, r["id"]) | {"superseded": r["superseded"], "conflicts": r["conflicts"]}


class MemoryPatch(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@router.patch("/memories/{mid}")
def patch_memory(mid: str, body: MemoryPatch, conn=Depends(db)):
    from jarvis.memory import service as M
    try:
        return M.correct(conn, mid, body.content)
    except KeyError as e:
        raise HTTPException(404) from e


@router.post("/memories/{mid}/forget")
def forget_memory(mid: str, conn=Depends(db)):
    from jarvis.memory import service as M
    M.forget(conn, mid)
    return {"ok": True, "note": "Removed from search and scrubbed locally. Existing backups may still contain it until deleted."}


class PinIn(BaseModel):
    pinned: bool


@router.post("/memories/{mid}/pin")
def pin_memory(mid: str, body: PinIn, conn=Depends(db)):
    from jarvis.memory import service as M
    M.set_pinned(conn, mid, body.pinned)
    return M.get(conn, mid)


@router.get("/memories/export")
def export_memories(conn=Depends(db)):
    from jarvis.memory import service as M
    return PlainTextResponse(M.export_markdown(conn), media_type="text/markdown",
                             headers={"Content-Disposition": "attachment; filename=jarvis-memory.md"})


@router.get("/projects")
def projects(conn=Depends(db)):
    out = rows(conn, "SELECT * FROM projects ORDER BY name")
    snap = ctx.tasks_snapshot(conn)
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


@router.patch("/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch, conn=Depends(db)):
    if body.notion_url and not body.notion_url.startswith("https://"):
        raise HTTPException(400, "Notion link must be https://")
    fields = body.model_dump(exclude_none=True)
    if fields:
        conn.execute(f"UPDATE projects SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=?",
                     (*fields.values(), now_iso(), pid))
    return row(conn, "SELECT * FROM projects WHERE id=?", (pid,))


@router.post("/documents")
async def upload_document(file: UploadFile = File(...), project_id: str | None = Form(None), conn=Depends(db)):
    import anyio

    from jarvis.knowledge.ingest import ingest_bytes
    data = await file.read(20 * 1024 * 1024 + 1)
    res = await anyio.to_thread.run_sync(lambda: ingest_bytes(conn, data, file.filename or "upload", file.content_type,
                                                              project_id or None))
    return res


@router.get("/documents")
def documents(conn=Depends(db)):
    out = rows(conn, "SELECT d.id, d.title, d.mime, d.origin, d.project_id, d.created_at, v.version, v.sha256, v.status, "
                     "v.processing, v.bytes, (SELECT COUNT(*) FROM evidence_segments s WHERE s.source_version_id=v.id) segments "
                     "FROM source_documents d LEFT JOIN source_versions v ON v.id=d.current_version_id WHERE d.deleted_at IS NULL "
                     "ORDER BY d.created_at DESC")
    for d in out:
        d["processing"] = jload(d["processing"], {})
    return out


@router.get("/documents/{doc_id}")
def document(doc_id: str, conn=Depends(db)):
    d = row(conn, "SELECT * FROM source_documents WHERE id=? AND deleted_at IS NULL", (doc_id,))
    if not d:
        raise HTTPException(404)
    v = row(conn, "SELECT * FROM source_versions WHERE id=?", (d["current_version_id"],))
    segs = rows(conn, "SELECT id, locator, kind, substr(text,1,300) excerpt FROM evidence_segments WHERE source_version_id=? "
                      "ORDER BY rowid", (v["id"],)) if v else []
    return d | {"version": v["version"] if v else None, "status": v["status"] if v else None,
                "processing": jload(v["processing"], {}) if v else {}, "segments": segs}


@router.delete("/documents/{doc_id}")
def delete_doc(doc_id: str, conn=Depends(db)):
    from jarvis.knowledge.ingest import delete_document
    return delete_document(conn, doc_id) | {"note": "Backups may retain it until deleted."}


@router.get("/search")
def search(q: str, project_id: str | None = None, conn=Depends(db)):
    from jarvis.knowledge.search import search as s
    return s(conn, q, project_id=project_id, limit=20, allowed_project_ids={project_id} if project_id else None)


class EvidenceIn(BaseModel):
    document_ids: list[str]
    question: str = ""


@router.post("/evidence/preview")
def evidence_preview(body: EvidenceIn, conn=Depends(db)):
    from jarvis.knowledge.vision import evidence_preview as ep
    return ep(conn, body.document_ids, body.question) | {"leaves_machine": True,
                                                          "destination": "Anthropic API (only when you ask)"}


# ================================================================ integrations
def _serr(e):
    raise HTTPException(400, {"code": e.code, "message": e.message})


@router.get("/integrations")
def integrations(conn=Depends(db)):
    from jarvis.integrations.service import list_integrations
    return list_integrations(conn)


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)


@router.post("/integrations/todoist")
def todoist_connect(body: TokenIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_todoist
    try:
        return connect_todoist(conn, body.token)
    except SetupError as e:
        _serr(e)


@router.get("/integrations/todoist/projects")
def todoist_projects(conn=Depends(db)):
    from jarvis.integrations import accounts as A
    from jarvis.integrations.service import todoist_projects as tp
    try:
        return tp(conn)
    except A.NotConnected as e:
        raise HTTPException(400, str(e)) from e


class TargetIn(BaseModel):
    project_id: str
    area_projects: dict[str, str] | None = None


@router.post("/integrations/todoist/target")
def todoist_target(body: TargetIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_todoist_target
    try:
        return set_todoist_target(conn, body.project_id, body.area_projects)
    except SetupError as e:
        _serr(e)


@router.post("/integrations/todoist/sync")
def todoist_sync(conn=Depends(db)):
    from jarvis.integrations.base import ProviderError
    from jarvis.integrations.service import todoist_sync as ts
    try:
        return ts(conn)
    except ProviderError as e:
        raise HTTPException(502, {"code": e.kind, "message": e.message}) from e


@router.get("/integrations/google/setup")
def google_setup():
    from jarvis.integrations.service import google_setup_info
    return google_setup_info()


class PurposeIn(BaseModel):
    purpose: str


@router.post("/integrations/google/start")
def google_start(body: PurposeIn, response: Response, conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import google_start as gs
    try:
        url, binding = gs(conn, body.purpose)
    except SetupError as e:
        _serr(e)
    # Lax (not Strict) so the browser sends it on the redirect back from Google; scoped to the callback path only.
    response.set_cookie(OAUTH_BINDING_COOKIE, binding, httponly=True, samesite="lax", secure=get_settings().remote_mode,
                        max_age=600, path=OAUTH_CALLBACK_PATH)
    return {"auth_url": url}


@router.get("/integrations/google/calendars")
def google_cals(conn=Depends(db)):
    from jarvis.integrations import accounts as A
    from jarvis.integrations.service import google_calendars
    try:
        return google_calendars(conn)
    except A.NotConnected as e:
        raise HTTPException(400, str(e)) from e


class CalSelIn(BaseModel):
    read: list[str]
    write_target: str | None = None


@router.post("/integrations/google/calendars")
def google_cals_set(body: CalSelIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_calendars
    try:
        return set_calendars(conn, body.read, body.write_target)
    except SetupError as e:
        _serr(e)


class IdsIn(BaseModel):
    ids: list[str]


@router.post("/integrations/drive/roots")
def drive_roots(body: IdsIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_drive_roots
    try:
        return set_drive_roots(conn, body.ids)
    except SetupError as e:
        _serr(e)


@router.post("/integrations/drive/ingest/{file_id}")
def drive_ingest(file_id: str, conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import drive_ingest as di
    try:
        return di(conn, file_id)
    except SetupError as e:
        _serr(e)


class UrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@router.post("/integrations/ics")
def ics_connect(body: UrlIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_ics
    try:
        return connect_ics(conn, body.url)
    except SetupError as e:
        _serr(e)


@router.post("/integrations/ics/refresh")
def ics_refresh(conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import ics_refresh as r
    try:
        return r(conn)
    except SetupError as e:
        _serr(e)


@router.get("/deadlines")
def deadlines(conn=Depends(db)):
    return rows(conn, "SELECT * FROM deadline_items WHERE status!='cancelled' ORDER BY local_date, start_at LIMIT 300")


@router.get("/course-mappings")
def course_mappings(conn=Depends(db)):
    return rows(conn, "SELECT * FROM course_mappings ORDER BY pattern")


class MappingIn(BaseModel):
    pattern: str
    course: str
    role: str
    confirmed: bool = True


@router.post("/course-mappings")
def add_mapping(body: MappingIn, conn=Depends(db)):
    if body.role not in ("own", "gta"):
        raise HTTPException(400, "role must be own or gta")
    conn.execute("INSERT INTO course_mappings (id, pattern, course, role, confirmed, created_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(pattern) DO UPDATE SET course=excluded.course, role=excluded.role, confirmed=excluded.confirmed",
                 (new_id("cm"), body.pattern.strip(), body.course.strip(), body.role, int(body.confirmed), now_iso()))
    return rows(conn, "SELECT * FROM course_mappings ORDER BY pattern")


class GitHubIn(BaseModel):
    token: str
    repos: list[str]


@router.post("/integrations/github")
def github_connect(body: GitHubIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_github
    try:
        return connect_github(conn, body.token, body.repos)
    except SetupError as e:
        _serr(e)


@router.get("/github/status")
def github_status(conn=Depends(db)):
    from jarvis.integrations.service import github_status as gs
    return gs(conn)


class KeyIn(BaseModel):
    api_key: str


@router.post("/integrations/anthropic")
def anthropic_connect(body: KeyIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, connect_anthropic
    try:
        return connect_anthropic(conn, body.api_key)
    except SetupError as e:
        _serr(e)


@router.delete("/integrations/{account_id}")
def disconnect(account_id: str, conn=Depends(db)):
    from jarvis.integrations import accounts as A
    a = A.get_account_by_id(conn, account_id)
    if not a:
        raise HTTPException(404)
    if a["provider"] == "google" and a["secret_ciphertext"]:
        import json

        from jarvis.integrations.google_oauth import revoke
        try:
            revoke(json.loads(A.read_secret(a)).get("refresh_token") or "")
        except Exception:
            pass
    A.disconnect(conn, account_id)
    return {"ok": True}


# ================================================================ local operator
class RootIn(BaseModel):
    path: str
    label: str
    writable: bool = False


@router.get("/localops/roots")
def roots(conn=Depends(db)):
    from jarvis.localops.files import list_roots
    return list_roots(conn)


@router.post("/localops/roots")
def add_root(body: RootIn, conn=Depends(db)):
    from jarvis.localops.files import FileOpError
    from jarvis.localops.files import add_root as ar
    try:
        return ar(conn, body.path, body.label, body.writable)
    except FileOpError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message}) from e


@router.delete("/localops/roots/{rid}")
def del_root(rid: str, conn=Depends(db)):
    from jarvis.localops.files import remove_root
    remove_root(conn, rid)
    return {"ok": True}


def _fop(fn, *a):
    from jarvis.localops.files import FileOpError
    try:
        return fn(*a)
    except FileOpError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message}) from e


@router.get("/localops/list")
def lo_list(root_id: str, path: str = "", conn=Depends(db)):
    from jarvis.localops import files as F
    return _fop(lambda: F.list_dir(F.get_root(conn, root_id), path))


@router.get("/localops/read")
def lo_read(root_id: str, path: str, conn=Depends(db)):
    from jarvis.localops import files as F
    return _fop(lambda: F.read_file(F.get_root(conn, root_id), path))


@router.get("/localops/search")
def lo_search(root_id: str, q: str, conn=Depends(db)):
    from jarvis.localops import files as F
    return _fop(lambda: F.search(F.get_root(conn, root_id), q))


# ================================================================ settings / budget / diagnostics / backups
@router.get("/settings")
def get_settings_(conn=Depends(db)):
    r = row(conn, "SELECT * FROM user_settings WHERE id=1")
    data = jload(r["data"], {})
    data.pop("budget", None)
    return {"display_name": r["display_name"], "timezone": r["timezone"], "version": r["version"], **data,
            "data_flow": ["Audio → local faster-whisper on this machine (never uploaded).",
                          "Typed/spoken commands are interpreted locally; only when the model is enabled are selected "
                          "prompts, excerpts and images sent to Anthropic.",
                          "Todoist/Google/GitHub calls go directly from this machine to those providers.",
                          "No telemetry or third-party tracking."]}


class SettingsPatch(BaseModel):
    display_name: str | None = None
    timezone: str | None = None
    voice: dict[str, Any] | None = None
    privacy: dict[str, Any] | None = None
    notifications: dict[str, Any] | None = None
    models: dict[str, str] | None = None


@router.patch("/settings")
def patch_settings(body: SettingsPatch, conn=Depends(db)):
    with tx(conn):
        r = row(conn, "SELECT * FROM user_settings WHERE id=1")
        data = jload(r["data"], {})
        for k in ("voice", "privacy", "notifications", "models"):
            v = getattr(body, k)
            if v is not None:
                data[k] = data.get(k, {}) | v
        if body.timezone:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(body.timezone)
            except Exception as e:
                raise HTTPException(400, "bad timezone") from e
        conn.execute("UPDATE user_settings SET display_name=COALESCE(?, display_name), timezone=COALESCE(?, timezone), data=?, "
                     "version=version+1, updated_at=? WHERE id=1", (body.display_name, body.timezone, jdump(data), now_iso()))
    return get_settings_(conn)


@router.get("/budget")
def budget(conn=Depends(db)):
    from jarvis.llm import budget as B
    return B.summary(conn)


class BudgetIn(BaseModel):
    paid_inference_enabled: bool | None = None
    background_paid_enabled: bool | None = None
    daily_limit_usd: float | None = Field(None, ge=0)
    monthly_limit_usd: float | None = Field(None, ge=0)
    max_output_tokens: int | None = Field(None, ge=100, le=64000)
    max_tool_calls: int | None = Field(None, ge=0, le=20)
    max_specialists: int | None = Field(None, ge=1, le=5)
    effort: str | None = None


@router.patch("/budget")
def patch_budget(body: BudgetIn, conn=Depends(db)):
    from jarvis.llm import budget as B
    patch = body.model_dump(exclude_unset=True)
    if patch.get("effort") and patch["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "bad effort")
    if patch.get("paid_inference_enabled") and patch.get("daily_limit_usd") is None and \
            B.budget_settings(conn).get("daily_limit_usd") is None:
        raise HTTPException(400, "Set a daily limit before enabling paid inference.")
    B.update_budget(conn, patch)
    audit(conn, "user", "budget.updated", f"Budget settings changed: {sorted(patch)}")
    return B.summary(conn)


@router.get("/doctor")
def doctor(conn=Depends(db)):
    from jarvis.doctor import checks
    return {"checks": checks(conn)}


@router.get("/metrics/latency")
def latency(conn=Depends(db)):
    out = {}
    for k in [r["kind"] for r in rows(conn, "SELECT DISTINCT kind FROM latency_metrics")]:
        ms = [r["ms"] for r in rows(conn, "SELECT ms FROM latency_metrics WHERE kind=? ORDER BY id DESC LIMIT 200", (k,))]
        srt = sorted(ms)
        out[k] = {"count": len(ms), "p50_ms": srt[len(srt) // 2], "p95_ms": srt[min(len(srt) - 1, int(len(srt) * 0.95))],
                  "last_ms": ms[0]}
    return out


@router.get("/activity")
def activity(limit: int = 100, conn=Depends(db)):
    ev = rows(conn, "SELECT created_at at, kind, message, run_id FROM run_events ORDER BY id DESC LIMIT ?", (min(limit, 500),))
    au = rows(conn, "SELECT at, kind, summary message, entity_type, entity_id FROM audit_events ORDER BY id DESC LIMIT ?",
              (min(limit, 500),))
    return sorted(ev + au, key=lambda e: e["at"], reverse=True)[:limit]


@router.post("/backups")
def make_backup():
    from jarvis.backup import create_backup
    return create_backup()


@router.get("/backups")
def backups():
    from jarvis.backup import list_backups
    return list_backups()


@router.post("/backups/{bid}/restore-preview")
def restore_preview(bid: str):
    from jarvis.backup import restore_preview as rp
    try:
        return rp(bid)
    except FileNotFoundError as e:
        raise HTTPException(404) from e


class ConfirmIn(BaseModel):
    confirm: bool


@router.post("/backups/{bid}/restore")
def restore(bid: str, body: ConfirmIn):
    from jarvis.backup import restore as rs
    if not body.confirm:
        raise HTTPException(400, "Confirmation required.")
    try:
        return rs(bid)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


_ = date
