"""Tool registry for the agent loop.

Every capability the model can call is a `Tool` registered here, with its risk class. The registry is the single source
for: the schemas sent to the model, dispatch, specialist labels, and the Help page's generated reference — so the
manual can never drift from what the agent can actually do.

Risk classes (enforced by what handlers are allowed to do, not by the prompt):
- read:    reads data; no external side effects.
- propose: appends ActionSpecs to the run; nothing is written until an approval (AJ or an AJ-authored policy rule).
- act:     runs inside the sandbox only (never the host); still bounded by mission scope and policy tiers.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

Risk = Literal["read", "propose", "act"]
TOOL_SEARCH = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
MODULES = ("planning", "knowledge", "workspace", "propose", "mail", "apps", "sandbox", "skill_tools")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]          # handler(run: AgentRun, **args) -> JSON-able
    risk: Risk
    family: str                          # specialist label shown in the UI
    manual: str = ""                     # plain-language help text (Help page); defaults to description
    example: str = ""                    # a phrase AJ might say that uses this tool
    available: Callable[[Any, str], bool] | None = None   # available(conn, uid); None = always offered
    deferred: bool = False               # hidden behind tool search until the model asks for it

    def schema(self) -> dict:
        s = {"name": self.name, "description": self.description, "input_schema": self.input_schema}
        return s | ({"defer_loading": True} if self.deferred else {})


REGISTRY: dict[str, Tool] = {}


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def tool(name: str, description: str, input_schema: dict, *, risk: Risk, family: str, manual: str = "", example: str = "",
         available: Callable[[Any, str], bool] | None = None, deferred: bool = False):
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise ValueError(f"duplicate tool {name}")
        REGISTRY[name] = Tool(name, description, input_schema, fn, risk, family, manual or description, example,
                              available, deferred)
        return fn
    return deco


def load_all() -> dict[str, Tool]:
    for m in MODULES:
        importlib.import_module(f"jarvis.agents.tools.{m}")
    return REGISTRY


def dynamic(conn, uid: str) -> list[Tool]:
    """Tools from admin-reviewed MCP servers and installed skills (built per request from the DB, never cached)."""
    from jarvis.agents.tools.mcp_tools import tools as mcp_tools
    from jarvis.agents.tools.skill_tools import tools as skill_tools
    out: list[Tool] = []
    for src in (mcp_tools, skill_tools):
        try:
            out += src(conn, uid)
        except Exception:   # noqa: BLE001 — a broken MCP config or skill folder must never take the agent down
            continue
    return out


def offered(conn, uid: str) -> list[Tool]:
    return [t for t in load_all().values() if t.available is None or t.available(conn, uid)] + dynamic(conn, uid)


def _connected_checks(conn, uid: str) -> dict[str, bool]:
    """Which integration families are live right now. Their tools load up front (no tool-search round trip, ~5 s each);
    only unconnected apps, MCP servers and skills stay behind tool search."""
    from jarvis.integrations import accounts as A
    out = {"email": bool(A.connected_mailboxes(conn, uid))}
    for fam, provider in (("notes", "notion"), ("messaging", "slack"), ("life", "spotify"), ("home", "homeassistant"),
                          ("travel", "routing"), ("finance", "simplefin"), ("canvas", "canvas")):
        out[fam] = A.app_connected(conn, uid, provider)
    try:
        from jarvis.agents.tools.sandbox import sandbox_available
        out["sandbox"] = sandbox_available(conn, uid)
    except Exception:  # noqa: BLE001
        out["sandbox"] = False
    return out


def _defer(t: Tool, live: dict[str, bool]) -> bool:
    if not t.deferred or t.name.startswith(("mcp__", "skill__")) or t.family == "skills":
        return t.deferred
    key = "canvas" if t.name.startswith("canvas") else t.family
    return not live.get(key, False)


def schemas(conn, uid: str, allow: Callable[[Tool], bool] | None = None) -> list[dict]:
    """Client tool definitions for one request. Deferred tools are only reachable through tool search, which keeps the
    prompt small (and cacheable) as integrations and MCP servers add dozens of tools."""
    tools = [t for t in offered(conn, uid) if allow is None or allow(t)]
    live = _connected_checks(conn, uid)
    out, any_deferred = [], False
    for t in tools:
        d = _defer(t, live)
        any_deferred |= d
        sch = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        out.append(sch | ({"defer_loading": True} if d else {}))
    if any_deferred:
        out.insert(0, dict(TOOL_SEARCH))
    return out


def get(name: str) -> Tool | None:
    return load_all().get(name)


def lookup(conn, uid: str, name: str) -> Tool | None:
    """Static tool, or a currently-offered MCP tool (re-derived from the DB at call time)."""
    t = get(name)
    if t is None and name.startswith(("mcp__", "skill__")):
        t = next((d for d in dynamic(conn, uid) if d.name == name), None)
    return t


def family(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    if name.startswith("skill__"):
        return "skills"
    t = get(name)
    return t.family if t else "chief"
