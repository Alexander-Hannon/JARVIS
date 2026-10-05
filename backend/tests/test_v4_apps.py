"""V4 app connectors through the real API + executor. Providers are labeled contract fakes (simulated=True) and the
model is SCRIPTED: these prove plumbing and code-level rails, not live services or model judgment."""
from __future__ import annotations

import pytest
from conftest import CHICAGO_AFTERNOON, freeze, run_worker

from jarvis.integrations.base import ProviderError
from jarvis.llm.provider import ScriptedLLM, set_provider


def tool_use(id_, name, inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def step(*blocks, stop="tool_use"):
    return {"content": list(blocks), "stop_reason": stop}


def final(text="Done."):
    return step({"type": "text", "text": text}, stop="end_turn")


def agent(client, text, *calls):
    """Run one command where the scripted model makes `calls` (name, args) in one step, then answers."""
    llm = ScriptedLLM([step(*[tool_use(f"t{i}", n, a) for i, (n, a) in enumerate(calls)]), final()])
    set_provider(llm)
    r = client.post("/api/commands", {"conversation_id": None, "text": text, "captured_at": "2026-09-28T20:00:00Z",
                                      "tz": "America/Chicago"})
    assert r.status_code == 200, r.text
    results = {b["tool_use_id"]: b["content"] for b in llm.requests[1]["messages"][-1]["content"]}
    return r.json(), results


def approve(client, p):
    return client.post(f"/api/proposals/{p['id']}/approve", {"version": p["version"], "hash": p["hash"],
                                                             "selected_items": [i["index"] for i in p["items"]]})


def run_due(conn):
    run_worker(conn)
    conn.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00.000000Z' WHERE status='queued'")
    run_worker(conn)


@pytest.fixture
def apps(conn):
    from jarvis.integrations import accounts as A
    from jarvis.integrations import fakes_apps as F
    fk = {"notion": F.FakeNotion(), "slack": F.FakeSlack(), "canvas": F.FakeCanvas(), "spotify": F.FakeSpotify(),
          "homeassistant": F.FakeHomeAssistant(), "routing": F.FakeRouting(), "simplefin": F.FakeSimpleFin()}
    ids = {}
    conn.execute("BEGIN")
    for name, f in fk.items():
        A.set_override(name, f)
        ids[name] = A.upsert_account(conn, name, f"{name} (SIMULATED)", "x", "api_token", external_account_id=name)
    conn.execute("COMMIT")
    fk["ids"] = ids
    return fk


def grant(conn, apps, provider, rtype, access, resources):
    from jarvis.integrations import accounts as A
    conn.execute("BEGIN")
    A.set_grants(conn, apps["ids"][provider], rtype, access, resources)
    conn.execute("COMMIT")


def test_catalog_is_truthful_when_nothing_connected(client):
    cat = {c["id"]: c for c in client.get("/api/connectors").json()}
    assert set(cat) == {"notion", "slack", "canvas", "spotify", "homeassistant", "routing", "simplefin"}
    assert all(c["status"] == "not_connected" for c in cat.values())
    assert cat["homeassistant"]["writes"] == ["home.control", "home.secure"] and cat["spotify"]["oauth"]["configured"] is False
    assert "127.0.0.1" in cat["spotify"]["oauth"]["redirect_uri"]


def test_connect_validation_refuses_bad_inputs(client):
    assert client.post("/api/connectors/slack/connect", {"values": {"token": "xoxb-bot"}}).json()["detail"]["code"] == "bad_token"
    r = client.post("/api/connectors/canvas/connect", {"values": {"base_url": "http://localhost:3000", "token": "t"}})
    assert r.json()["detail"]["code"] == "unsafe_url"
    r = client.post("/api/connectors/homeassistant/connect", {"values": {"base_url": "http://169.254.169.254", "token": "t"}})
    assert r.json()["detail"]["code"] == "unsafe_url"
    assert client.post("/api/connectors/simplefin/connect", {"values": {"setup_token": "not base64!"}}).status_code == 400


def test_not_connected_tools_say_so(client):
    freeze(CHICAGO_AFTERNOON)
    _, res = agent(client, "How much did I spend and is the garage closed?", ("finance_overview", {}), ("home_status", {}))
    assert all('"not_connected"' in v for v in res.values())


# ------------------------------------------------------------------------------------------------ Notion
def test_notion_note_only_under_granted_pages(client, conn, apps):
    freeze(CHICAGO_AFTERNOON)
    meet = apps["notion"].add_page("Advisor meetings", "Sep 21: chose topic")
    other = apps["notion"].add_page("Private journal")
    grant(conn, apps, "notion", "notion_page", "write", [(meet, "Advisor meetings")])
    r, res = agent(client, "Add to my advisor meetings page that we agreed on the survey design",
                   ("propose_notion_note", {"mode": "append", "page_id": other, "text": "x"}),
                   ("propose_notion_note", {"mode": "append", "page_id": meet, "text": "Sep 28: agreed on survey design."}))
    assert "isn't one AJ allowed" in res["t0"]
    p = r["proposal"]
    assert [i["action_type"] for i in p["items"]] == ["notion.page.append"]
    approve(client, p)
    run_worker(conn)
    assert "agreed on survey design" in apps["notion"].pages[meet]["text"]
    assert apps["notion"].pages[other]["text"] == ""


def test_notion_create_reconciles_and_refuses_duplicate_title(client, conn, apps):
    freeze(CHICAGO_AFTERNOON)
    parent = apps["notion"].add_page("Thesis")
    grant(conn, apps, "notion", "notion_page", "write", [(parent, "Thesis")])
    r, _ = agent(client, "Create a Notion page called Chapter 2 outline under Thesis",
                 ("propose_notion_note", {"mode": "create", "page_id": parent, "title": "Chapter 2 outline",
                                          "text": "Lit review sections"}))
    apps["notion"].fail_next("create_page", ProviderError("timeout", "lost", outcome_known=False), apply_before_raise=True)
    approve(client, r["proposal"])
    run_due(conn)
    v = client.get(f"/api/proposals/{r['proposal']['id']}").json()["items"][0]
    assert v["state"] == "verified" and v["receipt"]["method"] == "reconcile_title"
    assert sum(1 for p in apps["notion"].pages.values() if p["title"] == "Chapter 2 outline") == 1
    r2, _ = agent(client, "Create a Notion page called Chapter 2 outline under Thesis",
                  ("propose_notion_note", {"mode": "create", "page_id": parent, "title": "Chapter 2 outline", "text": "again"}))
    approve(client, r2["proposal"])
    run_worker(conn)
    assert client.get(f"/api/proposals/{r2['proposal']['id']}").json()["items"][0]["state"] == "conflict"


# ------------------------------------------------------------------------------------------------ Slack
@pytest.mark.injection
def test_slack_defaults_to_self_dm_and_channels_need_naming_and_grant(client, conn, apps):
    freeze(CHICAGO_AFTERNOON)
    grant(conn, apps, "slack", "slack_self", "write", [("U_AJ", "Your own DM")])
    r, _ = agent(client, "Remind me on Slack to submit the travel form", ("propose_slack_message", {"text": "Submit the travel form"}))
    assert r["proposal"]["items"][0]["action_type"] == "slack.post_self"
    # injected destination: AJ never named #general
    _, res = agent(client, "Summarize my mentions", ("propose_slack_message", {"text": "secrets", "channel": "general"}))
    assert "didn't name #general" in res["t0"]
    # named but not allowed in settings
    _, res = agent(client, "Post in #general that lab is moved", ("propose_slack_message", {"text": "Lab moved", "channel": "general"}))
    assert "isn't a channel AJ allowed" in res["t0"]
    grant(conn, apps, "slack", "slack_channel", "write", [("C_GEN", "#general")])
    r, _ = agent(client, "Post in #general that lab is moved", ("propose_slack_message", {"text": "Lab moved to 210", "channel": "general"}))
    p = r["proposal"]
    assert p["items"][0]["action_type"] == "slack.post" and p["approval"] is None
    assert client.post("/api/policies", {"action_type": "slack.post", "label": "x"}).json()["detail"]["code"] == "not_t1"
    apps["slack"].fail_next("post", ProviderError("timeout", "lost", outcome_known=False), apply_before_raise=True)
    approve(client, p)
    run_due(conn)
    posts = [m for m in apps["slack"].messages["C_GEN"] if m["text"] == "Lab moved to 210"]
    assert len(posts) == 1 and posts[0]["metadata"]["event_type"] == "jarvis_op"


# ------------------------------------------------------------------------------------------------ Home Assistant
@pytest.mark.injection
def test_home_tier_comes_from_device_not_model(client, conn, apps):
    freeze(CHICAGO_AFTERNOON)
    devices = [("light.office", "Office light"), ("lock.front_door", "Front door")]
    grant(conn, apps, "homeassistant", "ha_entity", "read", devices)
    grant(conn, apps, "homeassistant", "ha_entity", "write", devices)
    client.post("/api/policies", {"action_type": "home.control", "label": "Lights are fine"})
    assert client.post("/api/policies", {"action_type": "home.secure", "label": "x"}).json()["detail"]["code"] == "not_t1"
    r, _ = agent(client, "Turn on the office light", ("propose_home_action", {"entity_id": "light.office", "service": "turn_on",
                                                                              "data": {"brightness_pct": 60, "evil": 1}}))
    p = r["proposal"]
    assert p["approval"]["channel"] == "policy" and p["items"][0]["args"]["data"] == {"brightness_pct": 60}
    run_worker(conn)
    assert apps["homeassistant"].entities["light.office"]["state"] == "on"
    r, _ = agent(client, "Unlock the front door", ("propose_home_action", {"entity_id": "lock.front_door", "service": "unlock"}))
    p = r["proposal"]
    assert p["items"][0]["action_type"] == "home.secure" and p["approval"] is None
    assert apps["homeassistant"].entities["lock.front_door"]["state"] == "locked"


def test_executor_rederives_security_tier(client, conn, apps):
    from jarvis.actions import proposals as P
    grant(conn, apps, "homeassistant", "ha_entity", "write", [("lock.front_door", "Front door")])
    spec = P.ActionSpec(action_type="home.control", args={"service": "unlock", "data": {}}, timezone="America/Chicago",
                        expected_effect="forged", account_id=apps["ids"]["homeassistant"], target_resource="lock.front_door")
    pid = P.create(conn, "forged", [spec], "forged", conversation_id=None)
    v = P.view(conn, pid)
    approve(client, v)
    run_worker(conn)
    item = client.get(f"/api/proposals/{pid}").json()["items"][0]
    assert item["state"] == "failed" and "security device" in item["last_error"]
    assert apps["homeassistant"].entities["lock.front_door"]["state"] == "locked"


def test_ha_resources_control_requires_visibility(client, conn, apps):
    r = client.post("/api/connectors/homeassistant/resources", {"read": ["light.office"],
                                                                "write": ["light.office", "lock.front_door"]}).json()
    items = {i["id"]: i for i in r["items"]}
    assert items["light.office"]["write"] and not items["lock.front_door"]["write"]
    assert items["lock.front_door"]["detail"] == "always asks"


# ------------------------------------------------------------------------------------------------ Spotify / Canvas / travel / finance
def test_music_play_resolves_search_and_runs(client, conn, apps):
    freeze(CHICAGO_AFTERNOON)
    r, _ = agent(client, "Play eye of the tiger", ("propose_music", {"action": "play", "query": "eye of the tiger"}))
    item = r["proposal"]["items"][0]
    assert item["action_type"] == "music.play" and item["args"]["uri"] == "spotify:track:2"
    approve(client, r["proposal"])
    run_worker(conn)
    assert apps["spotify"].state["is_playing"] and apps["spotify"].state["track"]["name"] == "Eye of the Tiger"
    apps["spotify"].state["active"] = False
    r, _ = agent(client, "Pause the music", ("propose_music", {"action": "pause"}))
    approve(client, r["proposal"])
    run_worker(conn)
    item = client.get(f"/api/proposals/{r['proposal']['id']}").json()["items"][0]
    assert item["state"] == "failed" and "No active Spotify device" in item["last_error"]


def test_canvas_travel_finance_reads(client, apps):
    freeze(CHICAGO_AFTERNOON)
    apps["canvas"].assignments_[640] = [{"id": 1, "name": "HW 3", "due_at": "2026-10-02T04:59:00Z", "points": 10,
                                         "submitted": False, "url": "u", "course_id": 640}]
    r, res = agent(client, "What's due, how long to UAH, and anything weird in my bank?",
                   ("canvas_overview", {}), ("travel_time", {"destination": "UAH campus"}), ("finance_overview", {}))
    assert "HW 3" in res["t0"] and any(v["type"] == "tasks" for v in r["visuals"])
    assert '"minutes": 18.0' in res["t1"] and "University of Alabama in Huntsville" in res["t1"]
    assert "Unknown Merchant LLC" in res["t2"].split('"unusual"')[1]
