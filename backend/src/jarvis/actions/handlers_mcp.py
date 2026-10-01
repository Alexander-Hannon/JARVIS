"""Executor handler for MCP action tools. Re-checks AJ's review at execution time: the server must still be active and
unchanged, the tool still in `propose` mode, and an `mcp.call.auto` approval only counts while AJ keeps the tool at T1."""
from __future__ import annotations

from jarvis.actions.executor import Blocked
from jarvis.core.db import jload
from jarvis.integrations import mcp_hub as H
from jarvis.integrations.base import ProviderError


def h_mcp_call(conn, a: dict, reconcile_first: bool) -> dict:
    args = a["args"]
    srv = H.get(conn, args["server_id"])
    if not srv or srv["status"] != "active" or srv["pinned_hash"] != srv["discovered_hash"]:
        raise Blocked("failed", "That MCP server is disabled or changed since review; review it again first.")
    mode = jload(srv["tool_modes"], {}).get(args["tool"]) or {}
    if mode.get("mode") != "propose":
        raise Blocked("failed", f"{args['tool']} is no longer enabled for actions on {srv['name']}.")
    if a["action_type"] == "mcp.call.auto" and mode.get("tier") != 1:
        raise Blocked("failed", f"{args['tool']} is no longer marked safe to automate; approve it explicitly.")
    if reconcile_first:
        # MCP has no idempotency or read-back contract: an unclear outcome goes to AJ, never an automatic re-run.
        raise Blocked("failed", "The earlier attempt's outcome is unclear; check the result yourself before re-running.")
    try:
        r = H.call_remote(srv, args["tool"], args.get("arguments") or {}, write=True)
    except H.MCPError as e:
        raise Blocked("failed", e.message) from None
    if r["is_error"]:
        raise ProviderError("validation", f"{srv['name']}.{args['tool']} returned an error: {r['text'][:300]}")
    return {"ok": True, "method": "tool_result", "simulated": srv["name"] in H._overrides,
            "observed": {"result": r["text"][:1500]}, "message": f"{srv['name']}.{args['tool']} ran"}
