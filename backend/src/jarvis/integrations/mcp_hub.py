"""MCP client hub: AJ's MCP servers become JARVIS tools — behind review, hash pinning and the normal approval path.

Why not the Messages API's server-side MCP connector: it runs tools on Anthropic's side, bypassing the executor,
approvals and audit. Here every MCP call is made by JARVIS's own code:
- read tools (the server marks them `readOnlyHint` AND AJ enabled them as read) run directly; output is untrusted data.
- every other tool is proposal-only: `mcp.call` (T2, always ask) or `mcp.call.auto` (T1) when AJ promoted that tool,
  which is refused for tools the server marks `destructiveHint`.
- the tool list is hashed at review. Discovery or any call that sees a different hash marks the server `changed`
  and disables it until AJ reviews again ("rug pull" defense) — checked inside the same session as the call.
- stdio servers run a command AJ typed, with the env he provided, on this host (until the M3 sandbox can host them).
  Prefer HTTP servers; never paste a command from untrusted content.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import anyio

from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, canonical_hash, new_id, now_iso
from jarvis.integrations.base import ProviderError
from jarvis.security import secrets

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
CALL_TIMEOUT = 60
LIST_TIMEOUT = 30
MAX_OUTPUT = 8000
# Tests install in-process servers here: name -> MCPServer instance (labeled simulated).
_overrides: dict[str, Any] = {}


class MCPError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ------------------------------------------------------------------------------------------------ transport
def _target(srv: dict):
    if srv["name"] in _overrides:
        return _overrides[srv["name"]]
    sec = json.loads(secrets.decrypt(srv["secret_ciphertext"])) if srv.get("secret_ciphertext") else {}
    if srv["transport"] == "stdio":
        from mcp import StdioServerParameters
        return StdioServerParameters(command=srv["command"], args=jload(srv["args"], []), env=sec.get("env") or None)
    if sec.get("bearer"):
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        return streamable_http_client(srv["url"], http_client=create_mcp_http_client(
            headers={"Authorization": f"Bearer {sec['bearer']}"}))
    return srv["url"]


def canonical_tools(tools: list) -> list[dict]:
    out = []
    for t in sorted(tools, key=lambda t: t.name):
        a = t.annotations
        out.append({"name": t.name, "description": (t.description or "")[:2000], "input_schema": t.input_schema or {},
                    "annotations": {"read_only": bool(a and a.read_only_hint), "destructive": bool(a and a.destructive_hint),
                                    "idempotent": bool(a and a.idempotent_hint)}})
    return out


async def _session(srv: dict, fn):
    from mcp import Client
    async with Client(_target(srv), read_timeout_seconds=CALL_TIMEOUT) as c:
        return await fn(c)


def _run(srv: dict, fn, timeout: float, write: bool):
    async def go():
        with anyio.fail_after(timeout):
            return await _session(srv, fn)
    try:
        try:
            return anyio.run(go)
        except BaseExceptionGroup as eg:   # task groups inside the client wrap errors; surface the meaningful one
            inner = next((e for e in _leaves(eg) if isinstance(e, MCPError | ProviderError | TimeoutError)), None)
            raise inner or eg.exceptions[0] from None
    except TimeoutError:
        raise ProviderError("timeout", f"MCP server {srv['name']} timed out", outcome_known=not write) from None
    except ProviderError:
        raise
    except MCPError:
        raise
    except Exception as e:  # noqa: BLE001 — transport/process failures become a typed, logged-safe error
        raise ProviderError("network", f"MCP server {srv['name']} failed: {type(e).__name__}", outcome_known=not write) from None


def _leaves(eg: BaseExceptionGroup):
    for e in eg.exceptions:
        yield from (_leaves(e) if isinstance(e, BaseExceptionGroup) else [e])


def list_remote(srv: dict) -> list[dict]:
    async def fn(c):
        return canonical_tools((await c.list_tools()).tools)
    return _run(srv, fn, LIST_TIMEOUT, write=False)


def call_remote(srv: dict, tool: str, arguments: dict, *, write: bool) -> dict:
    """List + call in ONE session: if the definitions drifted from the pinned hash, refuse before calling."""
    async def fn(c):
        current = canonical_tools((await c.list_tools()).tools)
        if canonical_hash(current) != srv["pinned_hash"]:
            raise MCPError("changed", "The server's tools changed since you reviewed them.")
        r = await c.call_tool(tool, arguments)
        text = "\n".join(getattr(b, "text", "") or f"[{getattr(b, 'type', 'content')}]" for b in (r.content or []))
        return {"is_error": bool(getattr(r, "is_error", False)), "text": text[:MAX_OUTPUT],
                "structured": getattr(r, "structured_content", None)}
    try:
        return _run(srv, fn, CALL_TIMEOUT, write=write)
    except MCPError as e:
        if e.code == "changed":
            mark_changed(srv["id"])
        raise


# ------------------------------------------------------------------------------------------------ persistence
def get(conn, sid_or_name: str) -> dict | None:
    return row(conn, "SELECT * FROM mcp_servers WHERE id=? OR name=?", (sid_or_name, sid_or_name))


def mark_changed(sid: str) -> None:
    from jarvis.core.db import connection
    with connection() as c2, tx(c2):
        c2.execute("UPDATE mcp_servers SET status='changed', updated_at=? WHERE id=? AND status='active'", (now_iso(), sid))
        audit(c2, "system", "mcp.changed", "MCP server tools changed; disabled until reviewed", "mcp_server", sid)


def add(conn: sqlite3.Connection, name: str, transport: str, *, command: str | None = None, args: list[str] | None = None,
        url: str | None = None, env: dict[str, str] | None = None, bearer: str | None = None, by: str | None = None) -> dict:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise MCPError("bad_name", "Name: 2–24 chars, lowercase letters/digits/underscore, starting with a letter.")
    if get(conn, name):
        raise MCPError("exists", "A server with that name already exists.")
    if transport == "stdio":
        if not command or any(ch in command for ch in ";|&`$<>\n"):
            raise MCPError("bad_command", "Give the executable only (arguments separately); no shell syntax.")
    elif transport == "http":
        from jarvis.security.netguard import UnsafeURL, validate_url
        if not url:
            raise MCPError("bad_url", "An HTTP server needs a URL.")
        try:
            if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/", url):   # local dev servers may use plain http
                validate_url(url)
        except UnsafeURL as e:
            raise MCPError("bad_url", str(e)) from None
    else:
        raise MCPError("bad_transport", "transport must be stdio or http")
    sid = new_id("mcp")
    sec = {k: v for k, v in (("env", env or None), ("bearer", bearer or None)) if v}
    with tx(conn):
        conn.execute("INSERT INTO mcp_servers (id, name, transport, command, args, url, secret_ciphertext, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sid, name, transport, command, jdump(args or []), url, secrets.encrypt(json.dumps(sec)) if sec else None,
                      now_iso(), now_iso()))
        audit(conn, "user", "mcp.added", f"MCP server added: {name} ({transport})", "mcp_server", sid, uid=by)
    return discover(conn, sid)


def discover(conn: sqlite3.Connection, sid: str) -> dict:
    srv = get(conn, sid)
    if not srv:
        raise MCPError("not_found", "Unknown MCP server.")
    try:
        tools = list_remote(srv)
    except ProviderError as e:
        with tx(conn):
            conn.execute("UPDATE mcp_servers SET status='error', last_error=?, updated_at=? WHERE id=?",
                         (e.message[:300], now_iso(), sid))
        return view(conn, sid)
    h = canonical_hash(tools)
    status = srv["status"]
    if srv["pinned_hash"] is None:
        status = "pending_review"
    elif h != srv["pinned_hash"]:
        status = "changed"
    elif status in ("error", "changed"):
        status = "active"
    with tx(conn):
        conn.execute("UPDATE mcp_servers SET discovered=?, discovered_hash=?, status=?, last_error=NULL, updated_at=? "
                     "WHERE id=?", (jdump(tools), h, status, now_iso(), sid))
        if status == "changed" and srv["status"] != "changed":
            audit(conn, "system", "mcp.changed", f"{srv['name']}: tools changed; disabled until reviewed", "mcp_server", sid)
    return view(conn, sid)


def review(conn: sqlite3.Connection, sid: str, modes: dict[str, dict], by: str | None = None) -> dict:
    """An admin approves the CURRENT tool list: per-tool mode off|read|propose and tier (1 = may be auto-approved by a rule)."""
    srv = get(conn, sid)
    if not srv:
        raise MCPError("not_found", "Unknown MCP server.")
    tools = {t["name"]: t for t in jload(srv["discovered"], [])}
    clean: dict[str, dict] = {}
    for name, m in modes.items():
        t = tools.get(name)
        if t is None:
            raise MCPError("unknown_tool", f"{name} isn't in the server's current tool list.")
        mode, tier = m.get("mode", "off"), int(m.get("tier", 2))
        if mode not in ("off", "read", "propose"):
            raise MCPError("bad_mode", "mode must be off, read or propose")
        if mode == "read" and not t["annotations"]["read_only"]:
            raise MCPError("not_read_only", f"{name} isn't marked read-only by the server; use propose (approval) instead.")
        if mode == "propose" and tier == 1 and t["annotations"]["destructive"]:
            raise MCPError("destructive", f"{name} is marked destructive; it always needs your explicit approval.")
        clean[name] = {"mode": mode, "tier": 1 if (mode == "propose" and tier == 1) else 2}
    with tx(conn):
        conn.execute("UPDATE mcp_servers SET tool_modes=?, pinned_hash=?, status='active', reviewed_at=?, updated_at=? "
                     "WHERE id=?", (jdump(clean), srv["discovered_hash"], now_iso(), now_iso(), sid))
        audit(conn, "user", "mcp.reviewed", f"Reviewed {srv['name']}: " + ", ".join(f"{k}={v['mode']}" for k, v in clean.items()),
              "mcp_server", sid, {"hash": srv["discovered_hash"]}, uid=by)
    return view(conn, sid)


def set_enabled(conn, sid: str, enabled: bool, by: str | None = None) -> dict:
    srv = get(conn, sid)
    if not srv:
        raise MCPError("not_found", "Unknown MCP server.")
    new = ("active" if srv["pinned_hash"] == srv["discovered_hash"] and srv["pinned_hash"] else "pending_review") \
        if enabled else "disabled"
    with tx(conn):
        conn.execute("UPDATE mcp_servers SET status=?, updated_at=? WHERE id=?", (new, now_iso(), sid))
        audit(conn, "user", "mcp." + ("enabled" if enabled else "disabled"), srv["name"], "mcp_server", sid, uid=by)
    return view(conn, sid)


def remove(conn, sid: str, by: str | None = None) -> None:
    if not get(conn, sid):
        raise MCPError("not_found", "Unknown MCP server.")
    with tx(conn):
        conn.execute("DELETE FROM mcp_servers WHERE id=?", (sid,))
        audit(conn, "user", "mcp.removed", "MCP server removed", "mcp_server", sid, uid=by)


def view(conn, sid: str) -> dict:
    s = get(conn, sid)
    modes = jload(s["tool_modes"], {})
    return {"id": s["id"], "name": s["name"], "transport": s["transport"], "command": s["command"],
            "args": jload(s["args"], []), "url": s["url"], "has_secret": bool(s["secret_ciphertext"]), "status": s["status"],
            "simulated": s["name"] in _overrides, "last_error": s["last_error"], "reviewed_at": s["reviewed_at"],
            "pinned_hash": s["pinned_hash"], "discovered_hash": s["discovered_hash"],
            "tools": [t | {"mode": modes.get(t["name"], {}).get("mode", "off"), "tier": modes.get(t["name"], {}).get("tier", 2)}
                      for t in jload(s["discovered"], [])]}


def list_servers(conn) -> list[dict]:
    return [view(conn, r["id"]) for r in rows(conn, "SELECT id FROM mcp_servers ORDER BY name")]


def active_tools(conn) -> list[tuple[dict, dict, dict]]:
    """(server row, tool def, mode) for every enabled tool on an active, reviewed, unchanged server."""
    out = []
    for s in rows(conn, "SELECT * FROM mcp_servers WHERE status='active' AND pinned_hash IS NOT NULL "
                        "AND pinned_hash=discovered_hash"):
        modes = jload(s["tool_modes"], {})
        for t in jload(s["discovered"], []):
            m = modes.get(t["name"])
            if m and m["mode"] != "off":
                out.append((s, t, m))
    return out
