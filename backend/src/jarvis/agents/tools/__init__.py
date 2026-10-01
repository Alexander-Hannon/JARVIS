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
MODULES = ("planning", "knowledge", "workspace", "propose", "mail", "apps")


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
    available: Callable[[Any], bool] | None = None   # available(conn); None = always offered
    deferred: bool = False               # hidden behind tool search until the model asks for it

    def schema(self) -> dict:
        s = {"name": self.name, "description": self.description, "input_schema": self.input_schema}
        return s | ({"defer_loading": True} if self.deferred else {})


REGISTRY: dict[str, Tool] = {}


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def tool(name: str, description: str, input_schema: dict, *, risk: Risk, family: str, manual: str = "", example: str = "",
         available: Callable[[Any], bool] | None = None, deferred: bool = False):
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


def offered(conn) -> list[Tool]:
    return [t for t in load_all().values() if t.available is None or t.available(conn)]


def schemas(conn) -> list[dict]:
    """Client tool definitions for one request. Deferred tools are only reachable through tool search, which keeps the
    prompt small (and cacheable) as integrations and MCP servers add dozens of tools."""
    tools = offered(conn)
    out = [t.schema() for t in tools]
    if any(t.deferred for t in tools):
        out.insert(0, dict(TOOL_SEARCH))
    return out


def get(name: str) -> Tool | None:
    return load_all().get(name)


def family(name: str) -> str:
    t = get(name)
    return t.family if t else "chief"
