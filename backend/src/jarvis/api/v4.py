"""V4 routes: autonomy policies, undo, capability catalog. Session-authenticated (mounted with require_session)."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from jarvis.actions import policy
from jarvis.actions import proposals as P
from jarvis.api.app import db, require_session
from jarvis.config import get_settings
from jarvis.core.db import get_conn, jload, row, rows

STREAM_MAX_SECONDS = 600       # long missions can outlive this; the client reconnects with ?after=<last id>
STREAM_WAIT_FOR_RUN = 15       # the client opens the stream before its POST /commands creates the run

router = APIRouter(prefix="/api")
public = APIRouter(prefix="/api")      # OAuth callbacks only (cross-site redirect: no Strict session cookie arrives)
MS_BINDING_COOKIE = "jarvis_ms_oauth_binding"
MS_CALLBACK_PATH = "/api/integrations/microsoft/callback"


def _err(e: policy.PolicyError | P.ProposalError):
    raise HTTPException(e.status, {"code": e.code, "message": e.message})


class RuleIn(BaseModel):
    action_type: str
    label: str = Field(min_length=1, max_length=120)
    match: dict | None = None
    origins: list[str] | None = None
    daily_cap: int = 10
    expires_at: str | None = None


@router.get("/policies")
def policies(conn=Depends(db)):
    return {"rules": policy.list_rules(conn), "catalog": policy.catalog(), "undo_window_minutes":
            int(policy.UNDO_WINDOW.total_seconds() // 60)}


@router.post("/policies")
def create_policy(body: RuleIn, session=Depends(require_session), conn=Depends(db)):
    try:
        return policy.create_rule(conn, body.action_type, body.label, match=body.match, origins=body.origins,
                                  daily_cap=body.daily_cap, expires_at=body.expires_at, session_id=session["id"])
    except policy.PolicyError as e:
        _err(e)


@router.delete("/policies/{rule_id}")
def revoke_policy(rule_id: str, conn=Depends(db)):
    try:
        return policy.revoke_rule(conn, rule_id)
    except policy.PolicyError as e:
        _err(e)


@router.post("/actions/{action_id}/undo")
def undo_action(action_id: str, session=Depends(require_session), conn=Depends(db)):
    try:
        res = policy.undo(conn, action_id, session["id"])
    except (policy.PolicyError, P.ProposalError) as e:
        _err(e)
    return res | {"proposal": P.view(conn, res["proposal_id"])}


# ------------------------------------------------------------------------------------------------ runs
def _run_view(conn, run_id: str) -> dict | None:
    r = row(conn, "SELECT id, conversation_id, kind, status, specialists, cancel_requested, error, started_at, finished_at "
                  "FROM runs WHERE id=?", (run_id,))
    if r:
        r["specialists"] = jload(r["specialists"], [])
        r["cancel_requested"] = bool(r["cancel_requested"])
    return r


@router.get("/runs/{run_id}")
def get_run(run_id: str, conn=Depends(db)):
    r = _run_view(conn, run_id)
    if not r:
        raise HTTPException(404, {"code": "not_found", "message": "Run not found."})
    return r


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream(run_id: str, after: int) -> Iterator[str]:
    # Own connection: the request-scoped one is closed once the handler returns, while this generator keeps running.
    conn = get_conn()
    try:
        t0, last, ticks = time.monotonic(), after, 0
        yield ": open\n\n"
        while time.monotonic() - t0 < STREAM_MAX_SECONDS:
            run = _run_view(conn, run_id)
            if run is None and time.monotonic() - t0 > STREAM_WAIT_FOR_RUN:
                yield _sse("error", {"message": "run not found"})
                return
            for e in rows(conn, "SELECT id, kind, message, data, created_at FROM run_events WHERE run_id=? AND id>? ORDER BY id",
                          (run_id, last)):
                e["data"] = jload(e["data"], {})
                last = e["id"]
                yield f"id: {e['id']}\n" + _sse("run_event", e)
            if run is not None and run["status"] != "running":
                yield _sse("done", run)
                return
            ticks += 1
            if ticks % 30 == 0:
                yield ": keepalive\n\n"     # keeps proxies (Tailscale serve, phones on cellular) from idling it out
            time.sleep(0.3)
        yield _sse("timeout", {"after": last})
    finally:
        conn.close()


@router.get("/runs/{run_id}/stream")
def stream_run(run_id: str, after: int = 0):
    """Server-sent run events (route/tool/status/approval/verify) as they happen. Replaces 700 ms polling."""
    return StreamingResponse(_stream(run_id, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------------------------ mail
def _serr(e):
    raise HTTPException(400, {"code": e.code, "message": e.message})


@router.get("/integrations/microsoft/setup")
def microsoft_setup():
    from jarvis.integrations.service import microsoft_setup_info
    return microsoft_setup_info()


class MsStartIn(BaseModel):
    with_send: bool = False


@router.post("/integrations/microsoft/start")
def microsoft_start(body: MsStartIn, response: Response, conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import microsoft_start as ms
    try:
        url, binding = ms(conn, "mail", body.with_send)
    except SetupError as e:
        _serr(e)
    response.set_cookie(MS_BINDING_COOKIE, binding, httponly=True, samesite="lax", secure=get_settings().remote_mode,
                        max_age=600, path=MS_CALLBACK_PATH)
    return {"auth_url": url}


@public.get("/integrations/microsoft/callback")
def microsoft_callback(request: Request, state: str = "", code: str | None = None, error: str | None = None,
                       conn=Depends(db)):
    # Same model as the Google callback: single-use state + PKCE verifier server-side + Lax binding cookie.
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import microsoft_callback as cb
    try:
        cb(conn, state, code, error, request.cookies.get(MS_BINDING_COOKIE))
        resp = RedirectResponse("/settings?connected=microsoft", status_code=303)
    except SetupError as e:
        resp = RedirectResponse(f"/settings?connect_error={e.code}", status_code=303)
    resp.delete_cookie(MS_BINDING_COOKIE, path=MS_CALLBACK_PATH)
    return resp


class SendingIn(BaseModel):
    mailbox: str
    enabled: bool


@router.post("/integrations/mail/sending")
def mail_sending(body: SendingIn, conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_mail_sending
    try:
        return set_mail_sending(conn, body.mailbox, body.enabled)
    except SetupError as e:
        _serr(e)


@router.get("/inbox")
def inbox(unread_only: bool = True, limit: int = 25, q: str | None = None, conn=Depends(db)):
    from jarvis.mail import service as M
    return M.inbox(conn, unread_only=unread_only, limit=max(1, min(limit, 50)), query=q) | {"mailboxes": M.status(conn)}


@router.get("/inbox/thread")
def inbox_thread(ref: str, conn=Depends(db)):
    from jarvis.integrations.accounts import NotConnected
    from jarvis.integrations.base import ProviderError
    from jarvis.mail import service as M
    try:
        return M.read_thread(conn, ref)
    except M.MailError as e:
        raise HTTPException(404 if e.code == "not_found" else 400, {"code": e.code, "message": e.message}) from None
    except NotConnected as e:
        raise HTTPException(409, {"code": "not_connected", "message": str(e)}) from None
    except ProviderError as e:
        raise HTTPException(502, {"code": e.kind, "message": "The mail provider didn't respond."}) from None
