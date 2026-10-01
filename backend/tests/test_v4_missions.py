"""V4 missions: schedules (incl. DST), scoped background agent runs, budgets, origin-scoped auto-approval.
The model is SCRIPTED; providers are labeled fakes."""
from __future__ import annotations

from datetime import UTC, datetime

from conftest import CHICAGO_AFTERNOON, freeze, run_worker

from jarvis.llm.provider import ScriptedLLM, set_provider

INBOX = {"name": "Inbox watch", "goal": "Check my inbox for anything that needs my reply.",
         "schedule": {"kind": "interval", "minutes": 120}, "families": ["email"], "output": "quiet"}


def final(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def tool_step(name, args):
    return {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": args}], "stop_reason": "tool_use"}


def notifications(conn):
    return [dict(r) for r in conn.execute("SELECT kind, title, body FROM notifications WHERE kind='mission'")]


def test_validation(client):
    bad = client.post("/api/missions", {**INBOX, "schedule": {"kind": "interval", "minutes": 5}})
    assert bad.json()["detail"]["code"] == "bad_schedule"
    assert client.post("/api/missions", {**INBOX, "max_cost_usd": 50}).json()["detail"]["code"] == "bad_budget"
    assert client.post("/api/missions", {**INBOX, "goal": "hi"}).json()["detail"]["code"] == "bad_mission"


def test_schedule_math_handles_dst():
    from jarvis.missions.service import next_run
    # US DST ends Sun Nov 1 2026: 07:00 Chicago is 12:00Z before, 13:00Z after
    sat = datetime(2026, 10, 31, 13, 0, tzinfo=UTC)
    assert next_run({"kind": "daily", "at": "07:00"}, "America/Chicago", sat).isoformat() == "2026-11-01T13:00:00+00:00"
    assert next_run({"kind": "weekly", "at": "18:00", "days": [6]}, "America/Chicago", sat).isoformat() == "2026-11-02T00:00:00+00:00"
    assert next_run({"kind": "interval", "minutes": 90}, "UTC", sat).isoformat() == "2026-10-31T14:30:00+00:00"


def test_tick_enqueues_once_and_runs_scoped(client, conn):
    from jarvis.missions import service as M
    freeze(CHICAGO_AFTERNOON)
    m = client.post("/api/missions", INBOX).json()
    conn.execute("UPDATE missions SET next_run_at='2026-09-28T19:00:00.000000Z' WHERE id=?", (m["id"],))
    llm = ScriptedLLM([tool_step("propose_tasks", {"tasks": [{"content": "x"}]}), final(M.NOTHING)])
    set_provider(llm)
    assert M.tick(conn) == 1 and M.tick(conn) == 0                  # advanced past now: no double enqueue
    run_worker(conn)
    names = {t.get("name") for t in llm.requests[0]["tools"]}
    assert "check_inbox" in names and "get_calendar" not in names and "propose_email_draft" not in names
    assert "web_search" not in names
    out = {b["tool_use_id"]: b["content"] for b in llm.requests[1]["messages"][-1]["content"]}
    assert "outside this run's scope" in out["t1"]                   # named anyway: refused in code
    v = client.get("/api/missions").json()["missions"][0]
    assert v["last_status"] == "nothing_new" and notifications(conn) == []      # quiet: nothing found, no ping
    assert "BACKGROUND MISSION" in llm.requests[0]["system"]


def test_found_something_notifies_and_records_history(client, conn):
    freeze(CHICAGO_AFTERNOON)
    m = client.post("/api/missions", INBOX).json()
    set_provider(ScriptedLLM([final("Dr. Lee wants midterm grades by Friday.")]))
    client.post(f"/api/missions/{m['id']}/run")
    run_worker(conn)
    assert notifications(conn) == [{"kind": "mission", "title": "Inbox watch", "body": "Dr. Lee wants midterm grades by Friday."}]
    v = client.get("/api/missions").json()["missions"][0]
    assert v["runs"][0]["status"] == "done" and v["runs"][0]["summary"].startswith("Dr. Lee")
    # next run sees the previous report in its own thread
    llm = ScriptedLLM([final("NOTHING_NEW")])
    set_provider(llm)
    client.post(f"/api/missions/{m['id']}/run")
    run_worker(conn)
    assert any("Dr. Lee" in str(msg.get("content")) for msg in llm.requests[0]["messages"])


def test_mission_proposals_respect_origin_scoped_rules(client, conn, fakes):
    freeze(CHICAGO_AFTERNOON)
    m = client.post("/api/missions", {**INBOX, "name": "Planner", "families": ["tasks"], "allow_proposals": True,
                                      "goal": "Add a task to review my notes tomorrow.", "output": "notify"}).json()
    client.post("/api/policies", {"action_type": "task.create", "label": "Tasks (my commands only)"})
    set_provider(ScriptedLLM([tool_step("propose_tasks", {"tasks": [{"content": "Review notes", "when_phrase": "tomorrow"}]}),
                              final("Queued a task to review your notes.")]))
    client.post(f"/api/missions/{m['id']}/run")
    run_worker(conn)
    p = client.get("/api/proposals").json()[0]
    assert p["approval"] is None and p["status"] == "proposed"       # command-only rule doesn't cover missions
    assert "Proposal waiting" in notifications(conn)[0]["body"]
    client.post("/api/policies", {"action_type": "task.create", "label": "Tasks from missions too",
                                  "origins": ["command", "mission"]})
    set_provider(ScriptedLLM([tool_step("propose_tasks", {"tasks": [{"content": "Review notes", "when_phrase": "tomorrow"}]}),
                              final("Added.")]))
    client.post(f"/api/missions/{m['id']}/run")
    run_worker(conn)
    pid = client.get("/api/missions").json()["missions"][0]["runs"][0]["proposal_id"]
    p = client.get(f"/api/proposals/{pid}").json()
    assert p["approval"]["channel"] == "policy" and p["status"] == "verified"     # approved by rule, executed same pass


def test_budget_and_unavailable_model_are_reported(client, conn):
    freeze(CHICAGO_AFTERNOON)
    m = client.post("/api/missions", INBOX).json()
    client.post(f"/api/missions/{m['id']}/run")
    run_worker(conn)                                                  # no model configured at all
    v = client.get("/api/missions").json()["missions"][0]
    assert v["last_status"] == "failed" and "unavailable" in v["last_summary"]
    assert notifications(conn)                                        # quiet missions still surface failures
    conn.execute("INSERT INTO mission_runs (id, mission_id, started_at, status, cost_usd) VALUES ('mr_x', ?, ?, 'done', 9.0)",
                 (m["id"], "2026-09-28T19:00:00.000000Z"))
    set_provider(ScriptedLLM([final("x")]))
    client.post(f"/api/missions/{m['id']}/run")
    run_worker(conn)
    assert client.get("/api/missions").json()["missions"][0]["last_status"] == "skipped_budget"


def test_pause_and_delete(client, conn):
    m = client.post("/api/missions", INBOX).json()
    v = client.post(f"/api/missions/{m['id']}/enabled", {"enabled": False}).json()
    assert v["enabled"] is False and v["next_run_at"] is None
    client.delete(f"/api/missions/{m['id']}")
    assert client.get("/api/missions").json()["missions"] == []
