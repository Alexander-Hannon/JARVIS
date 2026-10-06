"""V4.1: speed (effort routing, connected tools load up front, streamed steps with live progress, fast mode billing)
and the sandbox shown inline in the feed. The Anthropic client is faked at the SDK boundary; nothing calls the API."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import CHICAGO_AFTERNOON, freeze

from jarvis.llm.provider import ScriptedLLM, set_provider


def _inp(text, attachments=None):
    return SimpleNamespace(text=text, attachments=attachments, user_id="usr_owner")


def test_effort_routing(client, conn):
    from jarvis.agents import agent as A
    assert A.choose_effort(conn, _inp("What's on my calendar tomorrow?"), A.COMMAND_SCOPE) == "low"
    assert A.choose_effort(conn, _inp("Add a task to call mom Friday"), A.COMMAND_SCOPE) == "low"
    assert A.choose_effort(conn, _inp("Research the best GPUs for ML under $800"), A.COMMAND_SCOPE) == "medium"
    assert A.choose_effort(conn, _inp("Look up the Alabama game but use the Workbench"), A.COMMAND_SCOPE) == "medium"
    assert A.choose_effort(conn, _inp("hi", ["doc1"]), A.COMMAND_SCOPE) == "medium"
    conn.execute("UPDATE user_settings SET data=json_set(data, '$.research', json('{\"effort\": \"high\"}')) "
                 "WHERE user_id='usr_owner'")
    assert A.choose_effort(conn, _inp("hi"), A.COMMAND_SCOPE) == "high"


def test_connected_app_tools_load_up_front(conn):
    from jarvis.agents import tools as T
    from jarvis.integrations import accounts as A
    from jarvis.integrations.fakes import FakeMail
    def deferred(name):
        return next(s for s in T.schemas(conn, "usr_owner") if s.get("name") == name).get("defer_loading", False)
    assert deferred("check_inbox") and deferred("home_status")         # not connected: behind tool search
    A.set_override("gmail", FakeMail("gmail"))
    assert not deferred("check_inbox") and deferred("home_status")     # connected: no lookup round trip


# ------------------------------------------------------------------------------------------------ streaming provider
class _Stream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


def _fake_client(monkeypatch, behaviours, seen):
    import anthropic

    class Msgs:
        def __init__(self, beta):
            self.beta = beta

        def stream(self, **kw):
            seen.append({"beta": self.beta, **{k: kw.get(k) for k in ("speed", "betas", "output_config")}})
            b = behaviours.pop(0)
            if isinstance(b, Exception):
                raise b
            return b

    class Client:
        def __init__(self, **kw):
            self.messages = Msgs(False)
            self.beta = SimpleNamespace(messages=Msgs(True))
    monkeypatch.setattr(anthropic, "Anthropic", Client)


def _final(speed=None):
    usage = SimpleNamespace(model_dump=lambda: {"input_tokens": 1000, "output_tokens": 500, "speed": speed,
                                                 "server_tool_use": {"web_search_requests": 1}})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="Bama by 7.")], stop_reason="end_turn", usage=usage)


@pytest.fixture
def paid(client, conn, monkeypatch):
    monkeypatch.setenv("JARVIS_ANTHROPIC_API_KEY", "sk-ant-test")
    from jarvis.config import reset_settings_cache
    reset_settings_cache()
    from jarvis.llm import budget
    budget.update_budget(conn, {"chosen": True, "paid_inference_enabled": True, "daily_limit_usd": 5, "monthly_limit_usd": 50})
    conn.execute("INSERT INTO runs (id, user_id, kind, status, started_at) VALUES ('run_t', 'usr_owner', 'command', 'running', "
                 "'2026-09-28T20:00:00Z')")
    yield
    reset_settings_cache()


def test_streamed_step_reports_progress_live_and_bills_correctly(conn, monkeypatch, paid):
    from jarvis.llm.provider import AnthropicProvider
    events = [SimpleNamespace(type="content_block_stop", content_block=SimpleNamespace(
        type="server_tool_use", name="web_search", input={"query": "Alabama odds"}))]
    seen: list = []
    _fake_client(monkeypatch, [_Stream(events, _final())], seen)
    out = AnthropicProvider().agent_step(conn, uid="usr_owner", system="s", messages=[{"role": "user", "content": "x"}],
                                         tools=[], run_id="run_t", effort="low")
    assert out["progress_emitted"] and seen[0]["beta"] is False and seen[0]["output_config"] == {"effort": "low"}
    msgs = [r["message"] for r in conn.execute("SELECT message FROM run_events WHERE run_id='run_t'")]
    assert msgs == ["Searching the web: Alabama odds"]
    cost = conn.execute("SELECT cost_usd FROM usage_records").fetchone()[0]
    assert cost == pytest.approx((1000 * 4 + 500 * 20) / 1e6 + 0.01)


def test_fast_mode_uses_beta_speed_bills_double_and_falls_back(conn, monkeypatch, paid):
    import anthropic
    import httpx

    from jarvis.llm.provider import AnthropicProvider
    conn.execute("UPDATE user_settings SET data=json_set(data, '$.research', json('{\"fast_mode\": true}')) "
                 "WHERE user_id='usr_owner'")
    seen: list = []
    _fake_client(monkeypatch, [_Stream([], _final("fast"))], seen)
    AnthropicProvider().agent_step(conn, uid="usr_owner", system="s", messages=[{"role": "user", "content": "x"}], tools=[],
                                   run_id="run_t")
    assert seen[0]["beta"] and seen[0]["speed"] == "fast" and seen[0]["betas"] == ["fast-mode-2026-02-01"]
    assert conn.execute("SELECT cost_usd FROM usage_records").fetchone()[0] == pytest.approx((1000 * 4 + 500 * 20) / 1e6 * 2 + 0.01)
    limited = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                                       body=None)
    seen.clear()
    _fake_client(monkeypatch, [limited, _Stream([], _final())], seen)
    out = AnthropicProvider().agent_step(conn, uid="usr_owner", system="s", messages=[{"role": "user", "content": "x"}],
                                         tools=[], run_id="run_t")
    assert [s["beta"] for s in seen] == [True, False] and out["stop_reason"] == "end_turn"


# ------------------------------------------------------------------------------------------------ sandbox in the feed
def test_sandbox_work_shows_inline_in_the_reply(client):
    from jarvis.sandbox import runtime as R
    from jarvis.sandbox.runtime import ExecResult
    rt = R.FakeRuntime()
    R.set_runtime(rt)
    try:
        freeze(CHICAGO_AFTERNOON)
        rt.outputs["python3 odds.py"] = ExecResult(0, "Bama -7.5\n", "", 80)
        llm = ScriptedLLM([
            {"content": [{"type": "tool_use", "id": "t1", "name": "sandbox_run", "input": {"command": "python3 odds.py"}},
                         {"type": "tool_use", "id": "t2", "name": "screenshot", "input": {}, "toolset_name": "computer"}],
             "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "Bama is favored by seven and a half."}], "stop_reason": "end_turn"}])
        set_provider(llm)
        r = client.post("/api/commands", {"conversation_id": None, "text": "Check the odds in the workbench",
                                          "captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago"}).json()
        card = next(v for v in r["visuals"] if v["type"] == "sandbox")
        assert card["steps"][0]["detail"] == "python3 odds.py" and "Bama -7.5" in card["steps"][0]["output"]
        assert card["shot_id"] and card["desktop_actions"] == 1
        shot_events = [e for e in r["events"] if e["data"].get("shot_id")]
        assert shot_events and client.get(f"/api/sandbox/shots/{card['shot_id']}").status_code == 200
        assert client.get("/api/sandbox/live.png").status_code == 200
    finally:
        R.set_runtime(None)


def test_live_view_is_empty_when_sandbox_stopped(client):
    from jarvis.sandbox import runtime as R
    rt = R.FakeRuntime()
    R.set_runtime(rt)
    try:
        assert client.get("/api/sandbox/live.png").status_code == 204
    finally:
        R.set_runtime(None)


def test_deadline_heads_up_is_a_clean_list(conn):
    from jarvis.headsup import split_deadline
    assert split_deadline("Ch 10 Mini Sim on Pricing [(MKT 601-01/601-01J Merged) (FA26) MARKETING STRATEGY & ANALYSIS]") == \
        ("Ch 10 Mini Sim on Pricing", "MKT 601")
    assert json.dumps(split_deadline("Assignment 4 [MSC 550-01]")) == '["Assignment 4", "MSC 550"]'


# ------------------------------------------------------------------------------------------------ plan stays current
def test_plan_progress_follows_completed_tasks(client, conn, fakes):
    """AJ: 'make sure the mission remains up to date as tasks get completed'. Completions through JARVIS count at once;
    completions made directly in Todoist count via its completed-tasks API; nothing is assumed done without evidence."""
    from jarvis.agents import context
    from jarvis.core.util import now_iso
    client.put("/api/plan/today", {"mission": "Clear the grading backlog", "constraints": [],
                             "priorities": ["Grade quizzes", "Email advisor", "Prep lab"],
                             "first_action": "Grade quizzes — first open window 1:00 PM"})
    plan = client.get("/api/today").json()["plan"]
    assert [p["done"] for p in plan["progress"]] == [False, False, False]
    assert plan["next_action"].startswith("Grade quizzes")
    # completed through JARVIS (task_links)
    conn.execute("INSERT INTO task_links (id, user_id, op_key, content, status, created_at, updated_at) VALUES "
                 "('tl1', 'usr_owner', 'op1', 'Grade quizzes', 'completed', ?, ?)", (now_iso(), now_iso()))
    context.invalidate()
    plan = client.get("/api/today").json()["plan"]
    assert [p["done"] for p in plan["progress"]] == [True, False, False] and plan["next_action"] == "Email advisor"
    # completed directly in Todoist
    tid = fakes["todoist"].external_create("Email advisor (overdue)")
    fakes["todoist"].external_complete(tid)
    context.invalidate()
    plan = client.get("/api/today?refresh=true").json()["plan"]
    assert [p["done"] for p in plan["progress"]] == [True, True, False] and plan["next_action"] == "Prep lab"
    conn.execute("INSERT INTO task_links (id, user_id, op_key, content, status, created_at, updated_at) VALUES "
                 "('tl2', 'usr_owner', 'op2', 'Prep lab', 'completed', ?, ?)", (now_iso(), now_iso()))
    context.invalidate()
    plan = client.get("/api/today").json()["plan"]
    assert plan["all_done"] is True and plan["next_action"] is None
