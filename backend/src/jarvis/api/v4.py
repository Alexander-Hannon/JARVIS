"""V4 routes: autonomy policies, undo, capability catalog, apps, MCP, missions, channels, sandbox, skills.
Mounted with require("use"); every handler acts for `me` (the signed-in user), admin surfaces add require(...)."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from jarvis.actions import policy
from jarvis.actions import proposals as P
from jarvis.api.app import db, principal, require
from jarvis.config import get_settings
from jarvis.security.principal import Principal
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
def policies(me: Principal = Depends(principal), conn=Depends(db)):
    return {"rules": policy.list_rules(conn, me.user_id), "catalog": policy.catalog(), "undo_window_minutes":
            int(policy.UNDO_WINDOW.total_seconds() // 60)}


@router.post("/policies")
def create_policy(body: RuleIn, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        return policy.create_rule(conn, me.user_id, body.action_type, body.label, match=body.match, origins=body.origins,
                                  daily_cap=body.daily_cap, expires_at=body.expires_at, session_id=me.session_id)
    except policy.PolicyError as e:
        _err(e)


@router.delete("/policies/{rule_id}")
def revoke_policy(rule_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        return policy.revoke_rule(conn, me.user_id, rule_id)
    except policy.PolicyError as e:
        _err(e)


@router.post("/actions/{action_id}/undo")
def undo_action(action_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    try:
        res = policy.undo(conn, me.user_id, action_id, me.session_id)
    except (policy.PolicyError, P.ProposalError) as e:
        _err(e)
    return res | {"proposal": P.view(conn, me.user_id, res["proposal_id"])}


# ------------------------------------------------------------------------------------------------ runs
def _run_view(conn, run_id: str, uid: str) -> dict | None:
    r = row(conn, "SELECT id, conversation_id, kind, status, specialists, cancel_requested, error, started_at, finished_at "
                  "FROM runs WHERE id=? AND user_id=?", (run_id, uid))
    if r:
        r["specialists"] = jload(r["specialists"], [])
        r["cancel_requested"] = bool(r["cancel_requested"])
    return r


@router.get("/runs/{run_id}")
def get_run(run_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    r = _run_view(conn, run_id, me.user_id)
    if not r:
        raise HTTPException(404, {"code": "not_found", "message": "Run not found."})
    return r


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream(run_id: str, after: int, uid: str) -> Iterator[str]:
    """Only the run's owner gets events: someone else's (or an unknown) run looks the same as one that never appears."""
    # Own connection: the request-scoped one is closed once the handler returns, while this generator keeps running.
    conn = get_conn()
    try:
        t0, last, ticks = time.monotonic(), after, 0
        yield ": open\n\n"
        while time.monotonic() - t0 < STREAM_MAX_SECONDS:
            run = _run_view(conn, run_id, uid)
            if run is None and time.monotonic() - t0 > STREAM_WAIT_FOR_RUN:
                yield _sse("error", {"message": "run not found"})
                return
            if run is None:
                time.sleep(0.3)
                continue
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
def stream_run(run_id: str, after: int = 0, me: Principal = Depends(principal)):
    """Server-sent run events (route/tool/status/approval/verify) as they happen. Replaces 700 ms polling."""
    return StreamingResponse(_stream(run_id, after, me.user_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------------------------ mail
def _serr(e):
    raise HTTPException(400, {"code": e.code, "message": e.message})


@router.get("/integrations/microsoft/setup")
def microsoft_setup(me: Principal = Depends(principal)):
    from jarvis.integrations.service import microsoft_setup_info
    return microsoft_setup_info()


class MsStartIn(BaseModel):
    with_send: bool = False


@router.post("/integrations/microsoft/start")
def microsoft_start(body: MsStartIn, request: Request, response: Response, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError
    from jarvis.integrations.service import microsoft_start as ms
    try:
        url, binding = ms(conn, me.user_id, "mail", body.with_send, request.headers.get("origin"))
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
        resp = RedirectResponse("/connections?connected=microsoft", status_code=303)
    except SetupError as e:
        resp = RedirectResponse(f"/connections?connect_error={e.code}", status_code=303)
    resp.delete_cookie(MS_BINDING_COOKIE, path=MS_CALLBACK_PATH)
    return resp


class SendingIn(BaseModel):
    mailbox: str
    enabled: bool


@router.post("/integrations/mail/sending")
def mail_sending(body: SendingIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.service import SetupError, set_mail_sending
    try:
        return set_mail_sending(conn, me.user_id, body.mailbox, body.enabled)
    except SetupError as e:
        _serr(e)


@router.get("/inbox")
def inbox(unread_only: bool = True, limit: int = 25, q: str | None = None, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.mail import service as M
    return M.inbox(conn, me.user_id, unread_only=unread_only, limit=max(1, min(limit, 50)), query=q) | \
        {"mailboxes": M.status(conn, me.user_id)}


@router.get("/inbox/thread")
def inbox_thread(ref: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations.accounts import NotConnected
    from jarvis.integrations.base import ProviderError
    from jarvis.mail import service as M
    try:
        return M.read_thread(conn, me.user_id, ref)
    except M.MailError as e:
        raise HTTPException(404 if e.code == "not_found" else 400, {"code": e.code, "message": e.message}) from None
    except NotConnected as e:
        raise HTTPException(409, {"code": "not_connected", "message": str(e)}) from None
    except ProviderError as e:
        raise HTTPException(502, {"code": e.kind, "message": "The mail provider didn't respond."}) from None


# ------------------------------------------------------------------------------------------------ vault indexing
class VaultIn(BaseModel):
    enabled: bool


@router.get("/vault")
def vault_status(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.knowledge import vault
    return vault.status(conn, me.user_id)


@router.post("/vault/roots/{root_id}")
def vault_toggle(root_id: str, body: VaultIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.core import jobs
    from jarvis.knowledge import vault
    from jarvis.localops.files import FileOpError
    try:
        vault.set_indexed(conn, me.user_id, root_id, body.enabled)
    except FileOpError as e:
        raise HTTPException(404 if e.code == "no_root" else 400, {"code": e.code, "message": e.message}) from None
    if body.enabled:
        jobs.enqueue(conn, "vault_index", {"root_id": root_id, "user_id": me.user_id},
                     dedupe_key=f"vault:{root_id}:now:{time.time_ns()}")
    return vault.status(conn, me.user_id)


@router.post("/vault/roots/{root_id}/reindex")
def vault_reindex(root_id: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.core import jobs
    from jarvis.knowledge import vault
    if root_id not in vault.indexed_roots(conn, me.user_id):
        raise HTTPException(404, {"code": "not_indexed", "message": "Turn on search indexing for this folder first."})
    jobs.enqueue(conn, "vault_index", {"root_id": root_id, "user_id": me.user_id},
                 dedupe_key=f"vault:{root_id}:now:{time.time_ns()}")
    return {"queued": True}


# ------------------------------------------------------------------------------------------------ app connectors
SPOTIFY_BINDING_COOKIE = "jarvis_spotify_oauth_binding"
SPOTIFY_CALLBACK_PATH = "/api/integrations/spotify/callback"


class ConnectIn(BaseModel):
    values: dict[str, str]


class ResourcesIn(BaseModel):
    read: list[str] | None = None
    write: list[str] | None = None


def _conn_err(e):
    from jarvis.integrations.accounts import NotConnected
    from jarvis.integrations.base import ProviderError
    if isinstance(e, NotConnected):
        raise HTTPException(409, {"code": "not_connected", "message": str(e)}) from None
    if isinstance(e, ProviderError):
        raise HTTPException(502, {"code": e.kind, "message": e.message}) from None
    _serr(e)


@router.get("/connectors")
def connectors_list(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import connectors as C
    return C.catalog(conn, me.user_id)


@router.post("/connectors/{cid}/connect")
def connector_connect(cid: str, body: ConnectIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import connectors as C
    from jarvis.integrations.service import SetupError
    try:
        return C.connect(conn, me.user_id, cid, body.values)
    except SetupError as e:
        _serr(e)


@router.get("/connectors/{cid}/resources")
def connector_resources(cid: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import connectors as C
    from jarvis.integrations.accounts import NotConnected
    from jarvis.integrations.base import ProviderError
    from jarvis.integrations.service import SetupError
    if cid not in C.CATALOG or not C.offered(cid):
        raise HTTPException(404)
    try:
        return C.resources(conn, me.user_id, cid)
    except (SetupError, NotConnected, ProviderError) as e:
        _conn_err(e)


@router.post("/connectors/{cid}/resources")
def connector_set_resources(cid: str, body: ResourcesIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import connectors as C
    from jarvis.integrations.accounts import NotConnected
    from jarvis.integrations.base import ProviderError
    from jarvis.integrations.service import SetupError
    if cid not in C.CATALOG or not C.offered(cid):
        raise HTTPException(404)
    try:
        return C.set_resources(conn, me.user_id, cid, {"read": body.read or [], "write": body.write or []})
    except (SetupError, NotConnected, ProviderError) as e:
        _conn_err(e)


@router.post("/connectors/spotify/start")
def spotify_start(response: Response, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import connectors as C
    from jarvis.integrations.service import SetupError
    try:
        url, binding = C.spotify_start(conn, me.user_id)
    except SetupError as e:
        _serr(e)
    response.set_cookie(SPOTIFY_BINDING_COOKIE, binding, httponly=True, samesite="lax", secure=get_settings().remote_mode,
                        max_age=600, path=SPOTIFY_CALLBACK_PATH)
    return {"auth_url": url}


@public.get("/integrations/spotify/callback")
def spotify_callback(request: Request, state: str = "", code: str | None = None, error: str | None = None,
                     conn=Depends(db)):
    from jarvis.integrations import connectors as C
    from jarvis.integrations.service import SetupError
    try:
        C.spotify_callback(conn, state, code, error, request.cookies.get(SPOTIFY_BINDING_COOKIE))
        resp = RedirectResponse("/connections?connected=spotify", status_code=303)
    except SetupError as e:
        resp = RedirectResponse(f"/connections?connect_error={e.code}", status_code=303)
    resp.delete_cookie(SPOTIFY_BINDING_COOKIE, path=SPOTIFY_CALLBACK_PATH)
    return resp


# ------------------------------------------------------------------------------------------------ MCP servers
class McpIn(BaseModel):
    name: str
    transport: str
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    bearer: str | None = None


class McpReviewIn(BaseModel):
    modes: dict[str, dict]


class EnabledIn(BaseModel):
    enabled: bool


def _mcp(fn, *a, **kw):
    from jarvis.integrations import mcp_hub as H
    try:
        return fn(*a, **kw)
    except H.MCPError as e:
        raise HTTPException(404 if e.code == "not_found" else 400, {"code": e.code, "message": e.message}) from None


@router.get("/mcp")
def mcp_list(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.integrations import mcp_hub as H
    return H.list_servers(conn)


@router.post("/mcp")
def mcp_add(body: McpIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.integrations import mcp_hub as H
    return _mcp(H.add, conn, body.name, body.transport, command=body.command, args=body.args, url=body.url, env=body.env,
                bearer=body.bearer, by=me.user_id)


@router.post("/mcp/{sid}/discover")
def mcp_discover(sid: str, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.integrations import mcp_hub as H
    return _mcp(H.discover, conn, sid)


@router.post("/mcp/{sid}/review")
def mcp_review(sid: str, body: McpReviewIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.integrations import mcp_hub as H
    return _mcp(H.review, conn, sid, body.modes, by=me.user_id)


@router.post("/mcp/{sid}/enabled")
def mcp_enabled(sid: str, body: EnabledIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.integrations import mcp_hub as H
    return _mcp(H.set_enabled, conn, sid, body.enabled, by=me.user_id)


@router.delete("/mcp/{sid}")
def mcp_remove(sid: str, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.integrations import mcp_hub as H
    _mcp(H.remove, conn, sid, by=me.user_id)
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ missions
def _msn(fn, *a):
    from jarvis.missions import service as M
    try:
        return fn(*a)
    except M.MissionError as e:
        raise HTTPException(404 if e.code == "not_found" else 400, {"code": e.code, "message": e.message}) from None


@router.get("/missions")
def missions_list(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.missions import service as M
    return {"missions": M.list_missions(conn, me.user_id), "templates": M.TEMPLATES, "families": M.ALL_FAMILIES,
            "read_only_families": M.READ_ONLY_FAMILIES}


@router.post("/missions")
def missions_create(body: dict, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.agents import context as ctx
    from jarvis.missions import service as M
    return _msn(M.create, conn, me.user_id, body, ctx.user_tz(conn, me.user_id))


@router.patch("/missions/{mid}")
def missions_update(mid: str, body: dict, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.missions import service as M
    return _msn(M.update, conn, me.user_id, mid, body)


@router.post("/missions/{mid}/enabled")
def missions_enabled(mid: str, body: EnabledIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.missions import service as M
    return _msn(M.set_enabled, conn, me.user_id, mid, body.enabled)


@router.post("/missions/{mid}/run")
def missions_run(mid: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.core import jobs
    from jarvis.missions import service as M
    _msn(M.get, conn, me.user_id, mid)
    jobs.enqueue(conn, "run_mission", {"mission_id": mid, "user_id": me.user_id, "manual": True}, dedupe_key=f"mission:{mid}:manual:{time.time_ns()}",
                 max_attempts=1)
    return {"queued": True}


@router.delete("/missions/{mid}")
def missions_delete(mid: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.missions import service as M
    _msn(M.delete, conn, me.user_id, mid)
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ Telegram channel
class TokenIn(BaseModel):
    token: str


class NotifyIn(BaseModel):
    notify: bool


def _ch(fn, *a):
    from jarvis.channels import telegram as TG
    try:
        return fn(*a)
    except TG.ChannelError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message}) from None


@router.get("/channels/telegram")
def telegram_status(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.channels import telegram as TG
    return TG.status(conn, me.user_id)


@router.post("/channels/telegram")
def telegram_connect(body: TokenIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    """The org's bot (admins). Each person then pairs their own chat."""
    from jarvis.channels import telegram as TG
    return _ch(TG.connect, conn, body.token, me.user_id)


@router.post("/channels/telegram/pair")
def telegram_pair(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.channels import telegram as TG
    return _ch(TG.start_pairing, conn, me.user_id)


@router.post("/channels/telegram/unpair")
def telegram_unpair(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.channels import telegram as TG
    return TG.unpair(conn, me.user_id)


@router.post("/channels/telegram/notify")
def telegram_notify(body: NotifyIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.channels import telegram as TG
    return TG.set_notify(conn, me.user_id, body.notify)


# ------------------------------------------------------------------------------------------------ sandbox / Workbench
def _sbx(fn, *a):
    from jarvis.sandbox import service as SB
    try:
        return fn(*a)
    except SB.SandboxError as e:
        raise HTTPException(409 if e.code == "unavailable" else 400, {"code": e.code, "message": e.message}) from None


@router.get("/sandbox")
def sandbox_status(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.sandbox import service as SB
    from jarvis.core import settings as S
    data = S.user_data(conn, me.user_id)
    return SB.status(conn, me.user_id) | {"computer_use": (data.get("sandbox") or {}).get("computer_use", True),
                                          "can_build": me.can("org.integrations")}


@router.post("/sandbox/start")
def sandbox_start(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.sandbox import service as SB
    _sbx(SB.ensure_started, conn, me.user_id)
    return SB.status(conn, me.user_id)


@router.post("/sandbox/stop")
def sandbox_stop(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.sandbox import service as SB
    SB.get_runtime(me.user_id).stop()
    SB._log(conn, me.user_id, None, "lifecycle", "Stopped by you")
    return SB.status(conn, me.user_id)


@router.post("/sandbox/reset")
def sandbox_reset(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.sandbox import service as SB
    SB.get_runtime(me.user_id).reset()
    SB.close_network(conn, me.user_id)
    SB._log(conn, me.user_id, None, "lifecycle", "Reset by you (workspace erased)")
    return SB.status(conn, me.user_id)


@router.post("/sandbox/build")
def sandbox_build(me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.core import jobs
    jobs.enqueue(conn, "sandbox_build", {}, dedupe_key=f"sbxbuild:{int(time.time() // 600)}", max_attempts=1)
    return {"queued": True, "note": "Building the sandbox image takes a few minutes; the worker does it in the background."}


@router.post("/sandbox/network/close")
def sandbox_close_network(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.sandbox import service as SB
    return SB.close_network(conn, me.user_id)


@router.post("/sandbox/settings")
def sandbox_settings(body: dict, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.core import settings as S
    S.update_user_data(conn, me.user_id, lambda d: d.setdefault("sandbox", {}).__setitem__(
        "computer_use", bool(body.get("computer_use", True))))
    return sandbox_status(me=me, conn=conn)


@router.get("/sandbox/history")
def sandbox_history(limit: int = 100, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.sandbox import service as SB
    return SB.history(conn, me.user_id, limit)


@router.get("/sandbox/live.png")
def sandbox_live(me: Principal = Depends(principal)):
    """A fresh capture of the sandbox desktop (not stored). 204 when the sandbox isn't running."""
    from jarvis.sandbox.runtime import SandboxUnavailable, get_runtime
    rt = get_runtime(me.user_id)
    if not rt.status().get("running"):
        return Response(status_code=204)
    try:
        png = rt.screenshot()
    except SandboxUnavailable:
        return Response(status_code=204)
    return Response(png, media_type="image/png")


@router.get("/sandbox/shots/{shot_id}")
def sandbox_shot(shot_id: str, me: Principal = Depends(principal)):
    from fastapi.responses import FileResponse

    from jarvis.sandbox import service as SB
    p = SB.shot_path(me.user_id, shot_id)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------------------------------------ skills
class SkillFolderIn(BaseModel):
    folder: str


def _sk(fn, *a):
    from jarvis.skills import service as SK
    try:
        return fn(*a)
    except SK.SkillError as e:
        raise HTTPException(404 if e.code == "not_found" else 400, {"code": e.code, "message": e.message}) from None


@router.get("/skills")
def skills_list(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.skills import service as SK
    return SK.list_skills(conn, me.user_id)


@router.post("/skills/install")
def skills_install(body: SkillFolderIn, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    """Skills are installed org-wide from a folder on the JARVIS host (admins)."""
    from jarvis.skills import service as SK
    return _sk(SK.install_from_folder, conn, body.folder, me.user_id)


@router.post("/skills/{name}/enabled")
def skills_enabled(name: str, body: EnabledIn, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.skills import service as SK
    return _sk(SK.set_enabled, conn, me.user_id, name, body.enabled)


@router.delete("/skills/{name}")
def skills_remove(name: str, me: Principal = Depends(require("org.integrations")), conn=Depends(db)):
    from jarvis.skills import service as SK
    _sk(SK.remove, conn, name, me.user_id)
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ Today heads-up, report, manual
@router.get("/headsup")
def headsup(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis import headsup as H
    return {"cards": H.cards(conn, me.user_id)}


@router.get("/report/weekly")
def weekly(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis import headsup as H
    return H.weekly_report(conn, me.user_id)


@router.get("/manual")
def manual(me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.agents.agent import assistant_name
    from jarvis.manual import service as MS
    return {"assistant_name": assistant_name(conn), "guides": MS.guides(), "reference": MS.reference(conn, me.user_id)}


@router.get("/manual/search")
def manual_search(q: str, me: Principal = Depends(principal), conn=Depends(db)):
    from jarvis.manual import service as MS
    return MS.search(conn, me.user_id, q, 8)
