"""V4 routes: autonomy policies, undo, capability catalog. Session-authenticated (mounted with require_session)."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jarvis.actions import policy
from jarvis.actions import proposals as P
from jarvis.api.app import db, require_session
from jarvis.core.db import get_conn, jload, row, rows

STREAM_MAX_SECONDS = 600       # long missions can outlive this; the client reconnects with ?after=<last id>
STREAM_WAIT_FOR_RUN = 15       # the client opens the stream before its POST /commands creates the run

router = APIRouter(prefix="/api")


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
