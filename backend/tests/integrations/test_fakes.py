import pytest

from jarvis.integrations.base import ProviderError
from jarvis.integrations.fakes import FakeGoogleCalendar, FakeTodoist
from jarvis.integrations.google_calendar import event_id_from_op_key


def test_fake_identity():
    assert FakeTodoist.simulated and FakeTodoist().name == "todoist"
    assert FakeGoogleCalendar.simulated and FakeGoogleCalendar().name == "google_calendar"


# ------------------------------------------------------------------ Todoist

def test_todoist_same_uuid_no_duplicate():
    t = FakeTodoist()
    a = t.create_task("op-1", {"content": "Essay", "description": "[jarvis:op=op-1]", "project_id": "proj-jarvis"})
    b = t.create_task("op-1", {"content": "Essay", "description": "[jarvis:op=op-1]", "project_id": "proj-jarvis"})
    assert a.id == b.id and len(t.list_tasks()) == 1
    c = t.create_task("op-2", {"content": "Essay"})  # intentional repeat: different op -> new task
    assert c.id != a.id and len(t.list_tasks()) == 2


def test_todoist_timeout_after_apply_then_reconcile_by_marker_and_retry_same_uuid():
    t = FakeTodoist()
    t.fail_next("create_task", ProviderError("timeout", "lost", outcome_known=False), apply_before_raise=True)
    args = {"content": "Essay", "description": "[jarvis:op=op-1]"}
    with pytest.raises(ProviderError) as e:
        t.create_task("op-1", args)
    assert e.value.outcome_known is False
    found = t.find_by_marker("[jarvis:op=op-1]")
    assert found is not None
    assert t.create_task("op-1", args).id == found.id and len(t.list_tasks()) == 1


def test_todoist_fault_before_apply_does_not_apply():
    t = FakeTodoist()
    t.fail_next("create_task", ProviderError("server", "boom", outcome_known=False))
    with pytest.raises(ProviderError):
        t.create_task("op-1", {"content": "x", "description": "[jarvis:op=op-1]"})
    assert t.find_by_marker("[jarvis:op=op-1]") is None
    assert t.create_task("op-1", {"content": "x"}).content == "x"  # uuid was not consumed


def test_todoist_states_and_external_edits():
    t = FakeTodoist()
    task = t.create_task("op-1", {"content": "Read ch. 3", "due_date": "2026-10-01", "priority": 4})
    assert task.due["date"] == "2026-10-01" and task.priority == 4 and task.url.endswith(task.id)
    t.external_edit(task.id, content="Read ch. 3-4")
    assert t.get_task(task.id).content == "Read ch. 3-4"
    t.external_complete(task.id, completed_at="2026-09-28T12:00:00Z")
    assert t.list_tasks() == [] and t.get_task(task.id).is_completed
    assert [x.id for x in t.list_completed("2026-09-28T00:00:00Z", "2026-09-29T00:00:00Z")] == [task.id]
    t.reopen_task("op-2", task.id)
    assert not t.get_task(task.id).is_completed
    t.close_task("op-3", task.id)
    t.reopen_task("op-2", task.id)  # replayed uuid: not re-executed
    assert t.get_task(task.id).is_completed
    t.external_delete(task.id)
    assert t.get_task(task.id) is None
    with pytest.raises(ProviderError) as e:
        t.update_task("op-4", task.id, {"content": "x"})
    assert e.value.kind == "not_found"
    t.delete_task("op-5", task.id)  # idempotent delete


def test_todoist_update_idempotent():
    t = FakeTodoist()
    task = t.create_task("op-1", {"content": "a"})
    t.update_task("u-1", task.id, {"content": "b"})
    t.external_edit(task.id, content="AJ changed it")
    assert t.update_task("u-1", task.id, {"content": "b"}).content == "AJ changed it"


# ------------------------------------------------------------------ Calendar

def body(op="op-1", start="2026-10-01T13:00:00Z", end="2026-10-01T14:00:00Z", **kw):
    return {"summary": "Focus", "start": {"dateTime": start}, "end": {"dateTime": end},
            "extendedProperties": {"private": {"jarvis_op": op}}, **kw}


def test_calendar_insert_conflict_semantics():
    c = FakeGoogleCalendar()
    eid = event_id_from_op_key("op-1")
    a = c.insert_event("primary", eid, body())
    assert c.insert_event("primary", eid, body()).id == a.id  # 409 -> ours -> existing
    with pytest.raises(ProviderError) as e:
        c.insert_event("primary", eid, body(op="other"))
    assert e.value.kind == "conflict"
    c.delete_event("primary", eid, None)
    with pytest.raises(ProviderError):
        c.insert_event("primary", eid, body())  # deleted id stays reserved (cancelled)


def test_calendar_etags_and_precondition():
    c = FakeGoogleCalendar()
    eid = event_id_from_op_key("op-1")
    ev = c.insert_event("primary", eid, body())
    ev2 = c.patch_event("primary", eid, {"summary": "Deep work"}, ev.etag)
    assert ev2.etag != ev.etag and ev2.summary == "Deep work"
    with pytest.raises(ProviderError) as e:
        c.patch_event("primary", eid, {"summary": "stale"}, ev.etag)
    assert e.value.kind == "precondition_failed"
    c.external_edit("primary", eid, summary="AJ moved it")
    with pytest.raises(ProviderError):
        c.delete_event("primary", eid, ev2.etag)
    current = c.get_event("primary", eid)
    c.delete_event("primary", eid, current.etag)
    assert c.get_event("primary", eid).status == "cancelled"
    c.delete_event("primary", eid, current.etag)  # already gone -> ok


def test_calendar_attendees_refused_and_readonly_calendar():
    c = FakeGoogleCalendar()
    c.add_calendar("shared", "Lab", access_role="reader")
    with pytest.raises(ProviderError) as e:
        c.insert_event("primary", event_id_from_op_key("x"), body(attendees=[{"email": "b@x.com"}]))
    assert e.value.kind == "unsupported"
    with pytest.raises(ProviderError) as e:
        c.insert_event("shared", event_id_from_op_key("x"), body())
    assert e.value.kind == "forbidden"


def test_calendar_freebusy_from_busy_events_only():
    c = FakeGoogleCalendar()
    c.add_event("primary", {"id": "e1", "summary": "Class", "start": {"dateTime": "2026-10-01T13:00:00Z"},
                            "end": {"dateTime": "2026-10-01T14:00:00Z"}})
    c.add_event("primary", {"id": "e2", "summary": "Overlap", "start": {"dateTime": "2026-10-01T13:30:00Z"},
                            "end": {"dateTime": "2026-10-01T15:00:00Z"}})
    c.add_event("primary", {"id": "e3", "summary": "Free", "transparency": "transparent",
                            "start": {"dateTime": "2026-10-01T16:00:00Z"}, "end": {"dateTime": "2026-10-01T17:00:00Z"}})
    c.add_event("primary", {"id": "e4", "summary": "Cancelled", "status": "cancelled",
                            "start": {"dateTime": "2026-10-01T18:00:00Z"}, "end": {"dateTime": "2026-10-01T19:00:00Z"}})
    fb = c.free_busy(["primary"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
    assert fb == {"primary": [{"start": "2026-10-01T13:00:00Z", "end": "2026-10-01T15:00:00Z"}]}
    evs = c.list_events("primary", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
    assert [e.id for e in evs] == ["e1", "e2", "e3"]


def test_calendar_fault_injection_applied_then_lost():
    c = FakeGoogleCalendar()
    eid = event_id_from_op_key("op-9")
    c.fail_next("insert_event", ProviderError("timeout", "lost", outcome_known=False), apply_before_raise=True)
    with pytest.raises(ProviderError) as e:
        c.insert_event("primary", eid, body(op="op-9"))
    assert not e.value.outcome_known
    assert c.get_event("primary", eid) is not None
    assert c.insert_event("primary", eid, body(op="op-9")).id == eid  # retry is idempotent
    assert ("insert_event", ("primary", eid, body(op="op-9"))) in c.calls
