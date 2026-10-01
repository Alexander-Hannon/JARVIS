"""V4 tiered autonomy + registries. Providers are labeled contract fakes (simulated=True)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import CHICAGO_AFTERNOON, freeze, run_worker

TWO_TASKS = "I need to grade IS 640 homework tomorrow and buy groceries tonight."


def cmd(client, text, conv=None):
    r = client.post("/api/commands", {"conversation_id": conv, "text": text, "captured_at": "2026-09-28T20:00:00Z",
                                      "tz": "America/Chicago"})
    assert r.status_code == 200, r.text
    return r.json()


def rule(client, action_type="task.create", **kw):
    return client.post("/api/policies", {"action_type": action_type, "label": kw.pop("label", "Inbox tasks"), **kw})


def test_rules_only_cover_t1_action_types(client):
    r = rule(client, "task.delete", label="nuke")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "not_t1"
    assert rule(client, "email.send").json()["detail"]["code"] == "unknown_action"
    assert rule(client, origins=["internet"]).json()["detail"]["code"] == "bad_origin"
    cat = {c["action_type"]: c for c in client.get("/api/policies").json()["catalog"]}
    assert cat["task.create"]["automatable"] and not cat["event.delete"]["automatable"]


def test_no_rule_means_manual_approval(client, fakes):
    freeze(CHICAGO_AFTERNOON)
    p = cmd(client, TWO_TASKS)["proposal"]
    assert p["status"] == "proposed" and p["approval"] is None


def test_rule_auto_approves_executes_and_undo_reverses(client, fakes, conn):
    freeze(CHICAGO_AFTERNOON)
    assert rule(client, match={"target_resource": "proj-jarvis"}).status_code == 200
    r = cmd(client, TWO_TASKS)
    p = r["proposal"]
    assert p["status"] == "queued" and p["approval"]["channel"] == "policy"
    assert "Approved automatically" in r["message"]["text"]
    run_worker(conn)
    assert len(fakes["todoist"].list_tasks()) == 2
    hist = client.get("/api/actions/history").json()
    first = hist[0]
    assert first["auto_approved"] and first["undo_until"]
    u = client.post(f"/api/actions/{first['action_id']}/undo")
    assert u.status_code == 200, u.text
    assert u.json()["proposal"]["items"][0]["action_type"] == "task.delete"
    again = client.post(f"/api/actions/{first['action_id']}/undo").json()
    assert again["idempotent"] is True                      # double-click never creates a second undo
    run_worker(conn)
    assert len(fakes["todoist"].list_tasks()) == 1
    acts = conn.execute("SELECT actor FROM audit_events WHERE kind='approval.auto'").fetchall()
    assert [a["actor"] for a in acts] == ["policy"]


def test_undo_window_expires(client, fakes, conn):
    freeze(CHICAGO_AFTERNOON)
    rule(client)
    cmd(client, TWO_TASKS)
    run_worker(conn)
    aid = client.get("/api/actions/history").json()[0]["action_id"]
    freeze(CHICAGO_AFTERNOON + timedelta(minutes=6))
    r = client.post(f"/api/actions/{aid}/undo")
    assert r.status_code == 410


def test_all_or_nothing_and_daily_cap(client, fakes):
    freeze(CHICAGO_AFTERNOON)
    rule(client, daily_cap=1)
    p = cmd(client, TWO_TASKS)["proposal"]          # two items, cap of one: AJ decides, nothing half-approved
    assert p["status"] == "proposed" and p["approval"] is None
    mixed = cmd(client, "Block 2 hours tomorrow morning for thesis writing and add a task to call mom tomorrow")["proposal"]
    assert mixed["approval"] is None


def test_rule_target_must_match(client, fakes):
    freeze(CHICAGO_AFTERNOON)
    rule(client, match={"target_resource": "some-other-project"})
    assert cmd(client, TWO_TASKS)["proposal"]["approval"] is None


def test_origin_scoping_and_pause(client, fakes, conn):
    from jarvis.actions import policy
    from jarvis.actions import proposals as P
    freeze(CHICAGO_AFTERNOON)
    rule(client)
    p = cmd(client, TWO_TASKS)["proposal"]
    client.post(f"/api/proposals/{p['id']}/reject")
    specs = P.specs_from_actions(P.actions_for_version(conn, P.current_version(conn, p["id"])["pv"]["id"]))
    mission_pid = P.create(conn, "from a mission", specs, "x", conversation_id=None, origin="mission")
    assert policy.try_auto_approve(conn, mission_pid) is None        # rule only granted for AJ's own commands
    client.post("/api/control/pause", {"reason": "test"})
    cmd_pid = P.create(conn, "while paused", specs, "x", conversation_id=None)
    assert policy.try_auto_approve(conn, cmd_pid) is None


def test_revoked_rule_stops_applying(client, fakes):
    freeze(CHICAGO_AFTERNOON)
    rid = rule(client).json()["id"]
    assert client.delete(f"/api/policies/{rid}").json()["active"] is False
    assert cmd(client, TWO_TASKS)["proposal"]["approval"] is None


# ------------------------------------------------------------------------------------------------ registries
def test_model_has_no_tool_that_approves_or_writes_policy():
    from jarvis.agents import tools as T
    reg = T.load_all()
    assert len(reg) >= 20
    for name, t in reg.items():
        assert t.risk in ("read", "propose", "act")
        assert not any(w in name for w in ("approve", "policy", "rule", "execute", "undo"))
        if name.startswith("propose_"):
            assert t.risk == "propose"


def test_deferred_tools_bring_tool_search(conn):
    from jarvis.agents import tools as T
    names = [s.get("name") for s in T.schemas(conn)]
    assert "tool_search_tool_bm25" not in names
    T.REGISTRY["zz_deferred_probe"] = T.Tool("zz_deferred_probe", "probe", T.obj({}), lambda run: {}, "read", "chief",
                                             deferred=True)
    try:
        sch = T.schemas(conn)
        assert sch[0]["type"] == "tool_search_tool_bm25_20251119"
        assert next(s for s in sch if s.get("name") == "zz_deferred_probe")["defer_loading"] is True
    finally:
        del T.REGISTRY["zz_deferred_probe"]


def test_action_registry_floors():
    from jarvis.actions import registry as R
    for at in R.REGISTRY.values():
        assert callable(at.resolve_handler())
        if at.undo:
            assert at.undo in R.REGISTRY
    with pytest.raises(ValueError):
        R.register(R.ActionType("x.send", "x", "jarvis.actions.executor:h_file", R.Tier.T1, "none", "Send",
                                reaches_others=True))


# ------------------------------------------------------------------------------------------------ runs
def test_run_view_and_event_stream(client, fakes):
    freeze(CHICAGO_AFTERNOON)
    r = cmd(client, TWO_TASKS)
    run = client.get(f"/api/runs/{r['run_id']}").json()
    assert run["status"] != "running" and run["kind"] == "command"
    body = client.get(f"/api/runs/{r['run_id']}/stream").text
    assert "event: run_event" in body and "Awaiting your approval" in body and "event: done" in body
    tail = client.get(f"/api/runs/{r['run_id']}/stream?after=999999999").text
    assert "event: run_event" not in tail and "event: done" in tail
    assert client.get("/api/runs/run_missing").status_code == 404
