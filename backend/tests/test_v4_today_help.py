"""V4 Help manual (generated from registries), manual search, Today heads-up cards, weekly report, activity filters."""
from __future__ import annotations


from conftest import CHICAGO_AFTERNOON, freeze, run_worker

from jarvis.llm.provider import ScriptedLLM, set_provider


def test_manual_is_generated_from_registries(client):
    m = client.get("/api/manual").json()
    assert m["assistant_name"] == "JARVIS" and len(m["guides"]) == 9
    assert "accounts" in {g["id"] for g in m["guides"]}
    caps = {c["family"]: c for c in m["reference"]["capabilities"]}
    email = {t["name"]: t for t in caps["email"]["tools"]}
    assert email["propose_email_draft"]["risk"] == "Asks before changing anything" and email["check_inbox"]["example"]
    from jarvis.agents import tools as T
    assert all(t.manual for t in T.load_all().values())
    acts = {a["action_type"]: a for a in m["reference"]["actions"]}
    assert acts["gmail.send"]["tier"] == 2 and acts["task.create"]["automatable"]
    assert {c["id"] for c in m["reference"]["connectors"]} >= {"notion", "slack"}


def test_manual_search_and_agent_tool(client):
    hits = client.get("/api/manual/search", params={"q": "how do I connect slack"}).json()
    assert hits and hits[0]["guide"] == "Connections"
    hits = client.get("/api/manual/search", params={"q": "lost my phone recovery codes"}).json()
    assert hits and hits[0]["guide"] == "People, roles and sign-in"
    llm = ScriptedLLM([{"content": [{"type": "tool_use", "id": "t1", "name": "search_manual",
                                     "input": {"query": "let JARVIS add tasks without asking"}}], "stop_reason": "tool_use"},
                       {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}])
    set_provider(llm)
    client.post("/api/commands", {"conversation_id": None, "text": "how do I stop approving every task?",
                                  "captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago"})
    out = llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert "Approvals and autonomy" in out


def test_headsup_cards(client, conn, fakes):
    from jarvis.integrations import accounts as A
    from jarvis.integrations.fakes import FakeMail
    from jarvis.integrations.fakes_apps import FakeRouting
    freeze(CHICAGO_AFTERNOON)
    assert client.get("/api/headsup").json()["cards"] == []
    fakes["calendar"].add_event("primary", {"id": "e9", "summary": "Advisor meeting", "location": "UAH campus",
                                            "start": {"dateTime": "2026-09-28T16:30:00-05:00"},
                                            "end": {"dateTime": "2026-09-28T17:00:00-05:00"}})
    A.set_override("routing", FakeRouting(minutes=20))
    fm = FakeMail("gmail")
    fm.external_receive("Dr. Lee <lee@uah.edu>", "Grades", "Can you send grades?")
    A.set_override("gmail", fm)
    client.post("/api/commands", {"conversation_id": None, "text": "remind me to email Yi Tan tomorrow",
                                  "captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago"})
    from jarvis.agents import context
    context.invalidate()
    cards = {c["kind"]: c for c in client.get("/api/headsup").json()["cards"]}
    assert cards["approvals"]["title"] == "1 waiting for your approval"
    assert cards["leave_by"]["title"] == "Leave by 4:02 PM for Advisor meeting"      # 4:30 - (20*1.15+5)
    assert cards["email"]["detail"] == "Dr. Lee"


def test_weekly_report_and_activity_filter(client, conn, fakes):
    freeze(CHICAGO_AFTERNOON)
    client.post("/api/policies", {"action_type": "task.create", "label": "tasks"})
    client.post("/api/commands", {"conversation_id": None, "text": "remind me to email Yi Tan tomorrow",
                                  "captured_at": "2026-09-28T20:00:00Z", "tz": "America/Chicago"})
    run_worker(conn)
    r = client.get("/api/report/weekly").json()
    assert r["actions_done"] == 1 and r["auto_approved"] == 1 and r["commands"] == 1 and r["minutes_saved_estimate"] == 3
    acts = client.get("/api/activity", params={"actor": "policy"}).json()
    assert acts and all(a["actor"] == "policy" for a in acts)
