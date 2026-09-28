import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from jarvis.integrations.base import ProviderError
from jarvis.integrations.todoist import TodoistAdapter, build_sync_args

BASE = "https://api.todoist.com/api/v1"


def task(tid="t1", **kw):
    return {"id": tid, "content": "Write report", "description": "", "project_id": "p1", "checked": False,
            "is_deleted": False, "priority": 1, "labels": [], "due": None, "duration": None,
            "parent_id": None, "updated_at": "2026-09-28T10:00:00Z", **kw}


def commands_of(request: httpx.Request) -> list[dict]:
    form = parse_qs(request.content.decode())
    return json.loads(form["commands"][0])


@pytest.fixture
def adapter():
    with httpx.Client() as c:
        yield TodoistAdapter("secret-token", client=c)


@respx.mock
def test_list_tasks_paginates_with_cursor_and_project_filter(adapter):
    route = respx.get(f"{BASE}/tasks").mock(side_effect=[
        httpx.Response(200, json={"results": [task("t1")], "next_cursor": "abc.def"}),
        httpx.Response(200, json={"results": [task("t2", due={"date": "2026-10-01T09:00:00", "string": "oct 1 9am"})],
                                  "next_cursor": None}),
    ])
    tasks = adapter.list_tasks("p1")
    assert [t.id for t in tasks] == ["t1", "t2"]
    first, second = route.calls[0].request, route.calls[1].request
    assert first.headers["Authorization"] == "Bearer secret-token"
    assert first.url.params["project_id"] == "p1" and "cursor" not in first.url.params
    assert second.url.params["cursor"] == "abc.def"
    assert tasks[1].due["date"] == "2026-10-01" and tasks[1].due["datetime"] == "2026-10-01T09:00:00"
    assert tasks[0].url == "https://app.todoist.com/app/task/t1"


@respx.mock
def test_list_completed_uses_items_key(adapter):
    route = respx.get(f"{BASE}/tasks/completed/by_completion_date").mock(
        return_value=httpx.Response(200, json={"items": [task("t9", checked=True)], "next_cursor": None}))
    done = adapter.list_completed("2026-09-01T00:00:00Z", "2026-09-28T00:00:00Z")
    assert done[0].id == "t9" and done[0].is_completed
    assert route.calls[0].request.url.params["since"] == "2026-09-01T00:00:00Z"


@respx.mock
def test_get_task_404_returns_none_and_deleted_returns_none(adapter):
    respx.get(f"{BASE}/tasks/gone").mock(return_value=httpx.Response(404, json={"error": "Task not found"}))
    respx.get(f"{BASE}/tasks/del").mock(return_value=httpx.Response(200, json=task("del", is_deleted=True)))
    assert adapter.get_task("gone") is None
    assert adapter.get_task("del") is None


@respx.mock
def test_create_task_sends_sync_command_with_op_uuid_and_reads_back(adapter):
    sync = respx.post(f"{BASE}/sync").mock(return_value=httpx.Response(200, json={
        "sync_status": {"op-1": "ok"}, "temp_id_mapping": {"op-1": "real-7"}}))
    respx.get(f"{BASE}/tasks/real-7").mock(return_value=httpx.Response(200, json=task(
        "real-7", description="[jarvis:op=op-1]", duration={"amount": 30, "unit": "minute"}, priority=4)))
    t = adapter.create_task("op-1", {"content": "Write report", "description": "[jarvis:op=op-1]",
                                     "project_id": "p1", "due_date": "2026-10-01", "due_lang": "en",
                                     "duration": 30, "duration_unit": "minute", "priority": 4, "labels": ["school"]})
    assert t.id == "real-7" and t.priority == 4 and t.duration == {"amount": 30, "unit": "minute"}
    req = sync.calls[0].request
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    (cmd,) = commands_of(req)
    assert cmd["type"] == "item_add" and cmd["uuid"] == "op-1" and cmd["temp_id"] == "op-1"
    assert cmd["args"]["due"] == {"date": "2026-10-01", "lang": "en"}
    assert cmd["args"]["duration"] == {"amount": 30, "unit": "minute"}
    assert cmd["args"]["labels"] == ["school"]


@respx.mock
def test_create_replay_without_mapping_recovers_by_marker(adapter):
    respx.post(f"{BASE}/sync").mock(return_value=httpx.Response(200, json={"sync_status": {"op-1": "ok"}}))
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json={
        "results": [task("x"), task("real-7", description="note [jarvis:op=op-1]")], "next_cursor": None}))
    t = adapter.create_task("op-1", {"content": "Write report", "description": "note [jarvis:op=op-1]"})
    assert t.id == "real-7"


def test_build_sync_args_datetime_and_validation():
    assert build_sync_args({"content": "a", "due_datetime": "2026-10-01T09:00"})["due"] == {"date": "2026-10-01T09:00:00"}
    fixed = build_sync_args({"content": "a", "due_datetime": "2026-10-01T09:00:00-04:00"})
    assert fixed["due"] == {"date": "2026-10-01T13:00:00Z"}
    assert build_sync_args({"content": "a", "due_string": "tomorrow"})["due"] == {"string": "tomorrow"}
    with pytest.raises(ProviderError) as e:
        build_sync_args({"content": "a", "due_date": "2026-10-01", "due_string": "x"})
    assert e.value.kind == "validation"
    with pytest.raises(ProviderError) as e:
        build_sync_args({"project_id": "p2"}, update=True)
    assert e.value.kind == "unsupported"
    with pytest.raises(ProviderError):
        build_sync_args({"content": "a", "bogus": 1})


@respx.mock
def test_update_close_reopen_delete_commands(adapter):
    sync = respx.post(f"{BASE}/sync").mock(side_effect=lambda req: httpx.Response(
        200, json={"sync_status": {c["uuid"]: "ok" for c in commands_of(req)}}))
    respx.get(f"{BASE}/tasks/t1").mock(return_value=httpx.Response(200, json=task("t1", content="New")))
    assert adapter.update_task("u1", "t1", {"content": "New", "duration": None}).content == "New"
    adapter.close_task("u2", "t1")
    adapter.reopen_task("u3", "t1")
    adapter.delete_task("u4", "t1")
    cmds = [commands_of(c.request)[0] for c in sync.calls]
    assert [(c["type"], c["uuid"]) for c in cmds] == [("item_update", "u1"), ("item_close", "u2"),
                                                      ("item_uncomplete", "u3"), ("item_delete", "u4")]
    assert cmds[0]["args"] == {"id": "t1", "content": "New", "duration": None}


@respx.mock
def test_sync_status_error_maps_to_validation_with_error_tag(adapter):
    respx.post(f"{BASE}/sync").mock(return_value=httpx.Response(200, json={"sync_status": {"u1": {
        "error_tag": "INVALID_ARGUMENT_VALUE", "error_code": 20, "error": "Invalid argument value", "http_code": 400}}}))
    with pytest.raises(ProviderError) as e:
        adapter.close_task("u1", "t1")
    assert e.value.kind == "validation" and e.value.outcome_known
    assert e.value.detail["error_tag"] == "INVALID_ARGUMENT_VALUE"


@respx.mock
def test_delete_of_missing_task_is_idempotent(adapter):
    respx.post(f"{BASE}/sync").mock(return_value=httpx.Response(200, json={"sync_status": {"u1": {
        "error_tag": "ITEM_NOT_FOUND", "error_code": 22, "error": "Item not found", "http_code": 404}}}))
    adapter.delete_task("u1", "t1")


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "forbidden"), (400, "validation")])
@respx.mock
def test_http_error_mapping(adapter, status, kind):
    respx.get(f"{BASE}/projects").mock(return_value=httpx.Response(status, json={"error": "x"}))
    with pytest.raises(ProviderError) as e:
        adapter.list_projects()
    assert e.value.kind == kind and e.value.status == status


@respx.mock
def test_429_retry_after(adapter):
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}, json={}))
    with pytest.raises(ProviderError) as e:
        adapter.list_tasks()
    assert e.value.kind == "rate_limit" and e.value.retry_after == 7.0 and e.value.retryable


@respx.mock
def test_5xx_on_write_outcome_unknown_but_known_on_read(adapter):
    respx.post(f"{BASE}/sync").mock(return_value=httpx.Response(502))
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError) as e:
        adapter.close_task("u1", "t1")
    assert e.value.kind == "server" and e.value.outcome_known is False
    with pytest.raises(ProviderError) as e:
        adapter.list_tasks()
    assert e.value.kind == "server" and e.value.outcome_known is True


@respx.mock
def test_timeout_on_write_outcome_unknown(adapter):
    respx.post(f"{BASE}/sync").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as e:
        adapter.create_task("op-1", {"content": "x"})
    assert e.value.kind == "timeout" and e.value.outcome_known is False


@respx.mock
def test_connect_error_on_write_outcome_known(adapter):
    respx.post(f"{BASE}/sync").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as e:
        adapter.close_task("u1", "t1")
    assert e.value.kind == "network" and e.value.outcome_known is True


@respx.mock
def test_read_error_after_send_on_write_outcome_unknown(adapter):
    respx.post(f"{BASE}/sync").mock(side_effect=httpx.ReadError("reset"))
    with pytest.raises(ProviderError) as e:
        adapter.close_task("u1", "t1")
    assert e.value.kind == "network" and e.value.outcome_known is False


@respx.mock
def test_find_by_marker(adapter):
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json={
        "results": [task("a"), task("b", description="x [jarvis:op=abc123]")], "next_cursor": None}))
    assert adapter.find_by_marker("[jarvis:op=abc123]").id == "b"
    assert adapter.find_by_marker("[jarvis:op=zzz]") is None


@respx.mock
def test_whoami_does_not_leak_token_fields(adapter):
    respx.get(f"{BASE}/user").mock(return_value=httpx.Response(200, json={
        "id": 42, "email": "aj@example.com", "full_name": "AJ", "token": "SECRET",
        "tz_info": {"timezone": "America/New_York"}, "inbox_project_id": "inb"}))
    me = adapter.whoami()
    assert me["email"] == "aj@example.com" and "SECRET" not in json.dumps(me)
    assert "secret-token" not in repr(adapter)
