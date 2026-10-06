"""Reasoning/vision provider interface with the official Anthropic SDK implementation.

- Typed outputs via `output_config.format` json_schema, then Pydantic validation + semantic checks by callers.
- Budget reservation before every call; measured usage reconciled after.
- Explicit error kinds: not_configured, budget, refusal, truncated, rate_limit, auth, server, network, invalid_output.
- A contract-mock provider (ScriptedLLM) records the exact request blocks for inspection and is labeled simulated.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from jarvis.config import get_settings
from jarvis.core.util import record_latency
from jarvis.llm import budget

log = logging.getLogger("jarvis.llm")


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


class LLMProvider:
    name = "base"
    simulated = False

    def available(self, conn) -> tuple[bool, str]:
        raise NotImplementedError

    def complete(self, conn: sqlite3.Connection, *, uid: str, purpose: str, system: str, content: list[dict] | str,
                 schema: dict | None = None, max_tokens: int | None = None, effort: str | None = None,
                 scheduled: bool = False, run_id: str | None = None, role: str = "chief") -> dict:
        """`uid` is who the call is for (spend is attributed to them).
        Returns {"text": str, "data": dict|None, "usage": dict, "model": str, "request_id": str|None}."""
        raise NotImplementedError


def _api_key(conn) -> str | None:
    s = get_settings()
    if s.anthropic_api_key:
        return s.anthropic_api_key
    from jarvis.integrations.accounts import org_account, read_secret
    acct = org_account(conn, "anthropic")      # one org-level key pays for the install
    return read_secret(acct) if acct else None


def _model_for(conn, role: str) -> str:
    from jarvis.core import settings as S
    return S.org_data(conn).get("models", {}).get(role) or get_settings().model_default


def estimate_input_tokens(system: str, content: list[dict] | str) -> int:
    chars = len(system)
    images = 0
    if isinstance(content, str):
        chars += len(content)
    else:
        for b in content:
            if b.get("type") == "text":
                chars += len(b["text"])
            elif b.get("type") in ("image", "document"):
                images += 1
    return chars // 3 + images * 1600 + 50


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def available(self, conn) -> tuple[bool, str]:
        if not _api_key(conn):
            return False, "No Anthropic API key configured (Settings → Integrations → Anthropic, or ANTHROPIC key in .env)."
        b = budget.budget_settings(conn)
        if not b.get("chosen") or not b.get("paid_inference_enabled"):
            return False, "Paid inference is off until a budget is chosen and enabled (Settings → Budget)."
        return True, "ok"

    def complete(self, conn, *, uid, purpose, system, content, schema=None, max_tokens=None, effort=None, scheduled=False,
                 run_id=None, role="chief") -> dict:
        import anthropic

        key = _api_key(conn)
        if not key:
            raise LLMError("not_configured", "No Anthropic API key configured.")
        b = budget.budget_settings(conn)
        model = _model_for(conn, role)
        max_tokens = min(max_tokens or 4000, int(b.get("max_output_tokens") or 4000))
        try:
            res_id = budget.reserve(conn, uid, purpose, model, estimate_input_tokens(system, content), max_tokens, scheduled,
                                    run_id)
        except budget.BudgetBlocked as e:
            raise LLMError("budget", e.message) from e
        client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=120.0)
        output_config: dict[str, Any] = {"effort": effort or b.get("effort") or "low"}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        msgs = [{"role": "user", "content": content}]
        t = time.perf_counter()
        try:
            resp = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=msgs,
                                          output_config=output_config)
        except anthropic.AuthenticationError as e:
            budget.release(conn, res_id)
            raise LLMError("auth", "Anthropic API key was rejected.") from e
        except anthropic.RateLimitError as e:
            budget.release(conn, res_id)
            raise LLMError("rate_limit", "Anthropic rate limit reached; try again shortly.") from e
        except anthropic.BadRequestError as e:
            budget.release(conn, res_id)
            raise LLMError("bad_request", f"Model request rejected: {e.message}") from e
        except anthropic.APIStatusError as e:
            budget.release(conn, res_id)
            raise LLMError("server", f"Model provider error ({e.status_code}).") from e
        except anthropic.APIConnectionError as e:
            budget.release(conn, res_id)
            raise LLMError("network", "Could not reach the model provider.") from e
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
        budget.reconcile(conn, res_id, model, usage, getattr(resp, "_request_id", None))
        record_latency(conn, "model_call", (time.perf_counter() - t) * 1000, {"purpose": purpose, "model": model})
        if resp.stop_reason == "refusal":
            raise LLMError("refusal", "The model declined this request.")
        text = "".join(bl.text for bl in resp.content if getattr(bl, "type", None) == "text")
        if resp.stop_reason == "max_tokens":
            raise LLMError("truncated", "Model output was truncated at the token limit; nothing was acted on.")
        data = None
        if schema:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise LLMError("invalid_output", "Model returned invalid structured output.") from e
        return {"text": text, "data": data, "usage": usage, "model": model, "request_id": getattr(resp, "_request_id", None)}


def _progress(conn, run_id: str | None, blk) -> None:
    """Report a server-side tool call the moment it completes (while the model keeps working)."""
    if not run_id:
        return
    from jarvis.core.util import run_event
    name = getattr(blk, "name", "")
    inp = getattr(blk, "input", None) or {}
    if name == "web_search":
        run_event(conn, run_id, "tool", f"Searching the web: {inp.get('query') or '…'}")
    elif name == "web_fetch":
        run_event(conn, run_id, "tool", f"Reading {inp.get('url') or 'a web page'}")
    else:
        run_event(conn, run_id, "tool", "Looking up the right tool")


def fast_mode_enabled(conn, uid: str) -> bool:
    from jarvis.core import settings as S
    return bool((S.user_data(conn, uid).get("research") or {}).get("fast_mode"))


def _agent_step_anthropic(self, conn, *, uid, system, messages, tools, max_tokens=4000, effort=None, run_id=None,
                          max_web_searches=0, scheduled=False) -> dict:
    """One model step of the agent loop, STREAMED so progress (web searches, page reads) shows up as it happens.
    Returns {"content": [blocks], "stop_reason", "usage", "model", "progress_emitted": True}. Content blocks are the
    SDK objects so thinking/server-tool blocks round-trip unchanged."""
    import anthropic

    key = _api_key(conn)
    if not key:
        raise LLMError("not_configured", "No Anthropic API key configured.")
    b = budget.budget_settings(conn)
    model = _model_for(conn, "agent")
    max_tokens = min(max_tokens, max(2000, int(b.get("max_output_tokens") or 4000)))
    est_in = sum(len(str(m.get("content"))) for m in messages) // 3 + len(system) // 3 + 2500
    fast = fast_mode_enabled(conn, uid)
    try:
        res_id = budget.reserve(conn, uid, "agent", model, est_in, max_tokens, scheduled, run_id, max_web_searches=max_web_searches,
                                multiplier=budget.FAST_MODE_MULT if fast else 1.0)
    except budget.BudgetBlocked as e:
        raise LLMError("budget", e.message) from e
    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=180.0)
    t = time.perf_counter()
    # Automatic prompt caching: tool definitions + system + prior steps are re-read at ~10% cost within a command.
    params = dict(model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools,
                  output_config={"effort": effort or "medium"}, cache_control={"type": "ephemeral"})

    def go(use_fast: bool):
        api = client.beta.messages if use_fast else client.messages
        extra = {"speed": "fast", "betas": ["fast-mode-2026-02-01"]} if use_fast else {}
        with api.stream(**params, **extra) as stream:
            for ev in stream:
                if ev.type == "content_block_stop" and getattr(ev.content_block, "type", "") == "server_tool_use":
                    _progress(conn, run_id, ev.content_block)
            return stream.get_final_message()
    try:
        try:
            resp = go(fast)
        except anthropic.RateLimitError:
            if not fast:
                raise
            resp = go(False)            # fast-mode capacity is separate; fall back to standard speed once
        except (TypeError, AttributeError, KeyError):
            # Defensive: if the SDK's streaming helper ever disagrees with this code, answer unstreamed rather than fail.
            log.exception("streamed agent step failed; retrying without streaming")
            resp = client.messages.create(**params)
    except anthropic.AuthenticationError as e:
        budget.release(conn, res_id)
        raise LLMError("auth", "Anthropic API key was rejected.") from e
    except anthropic.RateLimitError as e:
        budget.release(conn, res_id)
        raise LLMError("rate_limit", "Anthropic rate limit reached; try again shortly.") from e
    except anthropic.BadRequestError as e:
        budget.release(conn, res_id)
        raise LLMError("bad_request", f"Model request rejected: {e.message}") from e
    except anthropic.APIStatusError as e:
        budget.release(conn, res_id)
        raise LLMError("server", f"Model provider error ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        budget.release(conn, res_id)
        raise LLMError("network", "Could not reach the model provider.") from e
    usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
    budget.reconcile(conn, res_id, model, usage, getattr(resp, "_request_id", None))
    record_latency(conn, "model_call", (time.perf_counter() - t) * 1000, {"purpose": "agent", "model": model,
                                                                        "effort": effort or "medium", "fast": fast})
    return {"content": list(resp.content), "stop_reason": resp.stop_reason, "usage": usage, "model": model,
            "progress_emitted": True}


AnthropicProvider.agent_step = _agent_step_anthropic


class ScriptedLLM(LLMProvider):
    """Contract mock. Returns scripted outputs and records exact requests. NOT evidence of model accuracy."""

    name = "scripted-mock"
    simulated = True

    def __init__(self, responses: list[Any] | None = None, fn=None):
        self.responses = list(responses or [])
        self.fn = fn
        self.requests: list[dict] = []

    def available(self, conn) -> tuple[bool, str]:
        return True, "contract mock"

    def agent_step(self, conn, *, uid=None, system, messages, tools, max_tokens=4000, effort=None, run_id=None,
                   max_web_searches=0, scheduled=False):
        """Scripted agent steps: each queued response is {"content": [block dicts], "stop_reason": ...} or a callable(req)."""
        import copy
        req = {"system": system, "messages": copy.deepcopy(messages), "tools": tools,
               "content": copy.deepcopy(messages[-1]["content"]) if messages else None}
        self.requests.append(req)
        if not self.responses:
            raise LLMError("not_configured", "ScriptedLLM has no response queued.")
        out = self.responses.pop(0)
        if callable(out):
            out = out(req)
        if isinstance(out, LLMError):
            raise out
        if isinstance(out, str):
            out = {"content": [{"type": "text", "text": out}], "stop_reason": "end_turn"}
        return {"usage": {}, "model": "scripted-mock", **out}

    def complete(self, conn, *, uid=None, purpose, system, content, schema=None, max_tokens=None, effort=None,
                 scheduled=False, run_id=None, role="chief") -> dict:
        req = {"uid": uid, "purpose": purpose, "system": system, "content": content, "schema": schema, "scheduled": scheduled}
        self.requests.append(req)
        if self.fn:
            out = self.fn(req)
        elif self.responses:
            out = self.responses.pop(0)
        else:
            raise LLMError("not_configured", "ScriptedLLM has no response queued.")
        if isinstance(out, LLMError):
            raise out
        if isinstance(out, dict) and schema:
            return {"text": json.dumps(out), "data": out, "usage": {}, "model": "scripted-mock", "request_id": None}
        return {"text": str(out), "data": None, "usage": {}, "model": "scripted-mock", "request_id": None}


_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = AnthropicProvider()
    return _provider


def set_provider(p: LLMProvider | None) -> None:
    global _provider
    _provider = p
