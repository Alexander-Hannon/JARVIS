"""In-memory provider fakes that emulate real provider semantics for domain/executor tests.

Every fake exposes ``simulated = True``. They are never used against real accounts.

FakeTodoist emulates Todoist API v1:
* command ``uuid`` idempotency -- a replayed op_uuid is not re-executed; create returns the same task;
* get_task -> None for deleted/missing; list_tasks returns active tasks only; completed tasks via
  list_completed; marker search over active tasks' description/content;
* ``external_*`` helpers simulate AJ editing/completing/deleting/creating tasks in the Todoist app.

FakeGoogleCalendar emulates Calendar v3:
* client-id insert: an existing id -> 409 semantics (returns existing iff same private ``jarvis_op``
  and not cancelled, else ProviderError('conflict')); deleted events stay as status "cancelled";
* etags change on every write; If-Match mismatch -> precondition_failed;
* free/busy is computed from busy events (non-cancelled, opaque), merged, clipped to the window;
* all-day ``date`` values are treated as UTC midnight (approximation; the fake has no calendar tz).

Fault injection: ``fail_next(method, error, apply_before_raise=False)``. With
``apply_before_raise=True`` the write is applied (and its uuid recorded) before the error is raised --
use with a timeout error (outcome_known=False) to simulate "applied at provider, response lost".
"""
from __future__ import annotations

import copy
import itertools
from collections import defaultdict, deque
from datetime import UTC, date, datetime
from typing import Any

from .base import ProviderError, RemoteEvent, RemoteTask
from .google_calendar import EVENT_ID_RE, OP_PROP, check_event_body, parse_event
from .todoist import TASK_URL, build_sync_args, parse_task


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _FaultMixin:
    def _init_faults(self) -> None:
        self._faults: dict[str, deque[tuple[ProviderError, bool]]] = defaultdict(deque)
        self.calls: list[tuple[str, tuple]] = []

    def fail_next(self, method: str, error: ProviderError, apply_before_raise: bool = False) -> None:
        if not hasattr(self, method):
            raise AttributeError(method)
        self._faults[method].append((error, apply_before_raise))

    def _fault(self, method: str, *args: Any) -> tuple[ProviderError, bool] | None:
        self.calls.append((method, args))
        q = self._faults.get(method)
        return q.popleft() if q else None

    def _run(self, method: str, args: tuple, fn: Any) -> Any:
        fault = self._fault(method, *args)
        if fault and not fault[1]:
            raise fault[0]
        result = fn()
        if fault:
            raise fault[0]
        return result


# =========================================================================== Todoist

class FakeTodoist(_FaultMixin):
    name = "todoist"
    simulated = True

    def __init__(self) -> None:
        self._init_faults()
        self._ids = itertools.count(1)
        self.tasks: dict[str, dict] = {}          # raw API-shaped task dicts
        self.executed: dict[str, Any] = {}        # command uuid -> result (task id)
        self.projects: list[dict] = [{"id": "inbox", "name": "Inbox", "is_inbox_project": True},
                                     {"id": "proj-jarvis", "name": "Jarvis", "is_inbox_project": False}]

    # -- internal
    def _new_id(self) -> str:
        return f"t{next(self._ids):06d}"

    @staticmethod
    def _due_out(due_arg: dict | None) -> dict | None:
        if due_arg is None:
            return None
        return {"date": due_arg.get("date"), "timezone": due_arg.get("timezone"),
                "string": due_arg.get("string") or due_arg.get("date"), "lang": due_arg.get("lang", "en"),
                "is_recurring": False}

    def _apply_args(self, raw: dict, sync_args: dict) -> None:
        for k, v in sync_args.items():
            raw[k] = self._due_out(v) if k == "due" else copy.deepcopy(v)
        raw["updated_at"] = _now_iso()

    def _live(self, task_id: str) -> dict:
        raw = self.tasks.get(task_id)
        if raw is None or raw["is_deleted"]:
            raise ProviderError("not_found", f"task {task_id} not found", status=404,
                                detail={"error_tag": "ITEM_NOT_FOUND", "http_code": 404})
        return raw

    def _make(self, sync_args: dict) -> dict:
        tid = self._new_id()
        raw = {"id": tid, "content": "", "description": "", "project_id": "inbox", "parent_id": None,
               "due": None, "duration": None, "priority": 1, "labels": [], "checked": False, "is_deleted": False,
               "completed_at": None, "added_at": _now_iso(), "updated_at": _now_iso(), "url": TASK_URL.format(id=tid)}
        self._apply_args(raw, sync_args)
        self.tasks[tid] = raw
        return raw

    def _command(self, method: str, op_uuid: str, args: tuple, fn: Any) -> Any:
        """Todoist semantics: a previously executed uuid is not executed again."""
        def once() -> Any:
            if op_uuid in self.executed:
                return self.executed[op_uuid]
            res = fn()
            self.executed[op_uuid] = res
            return res
        return self._run(method, (op_uuid, *args), once)

    # -- reads
    def whoami(self) -> dict:
        return self._run("whoami", (), lambda: {"id": "fake-user", "email": "aj@example.invalid",
                                                "full_name": "AJ (simulated)", "timezone": "America/New_York",
                                                "inbox_project_id": "inbox"})

    def list_projects(self) -> list[dict]:
        return self._run("list_projects", (), lambda: copy.deepcopy(self.projects))

    def list_tasks(self, project_id: str | None = None) -> list[RemoteTask]:
        return self._run("list_tasks", (project_id,), lambda: [
            parse_task(copy.deepcopy(t)) for t in self.tasks.values()
            if not t["checked"] and not t["is_deleted"] and (project_id is None or t["project_id"] == project_id)])

    def get_task(self, task_id: str) -> RemoteTask | None:
        def fn() -> RemoteTask | None:
            raw = self.tasks.get(task_id)
            return None if raw is None or raw["is_deleted"] else parse_task(copy.deepcopy(raw))
        return self._run("get_task", (task_id,), fn)

    def list_completed(self, since_iso: str, until_iso: str) -> list[RemoteTask]:
        lo, hi = _parse_dt(since_iso), _parse_dt(until_iso)
        return self._run("list_completed", (since_iso, until_iso), lambda: [
            parse_task(copy.deepcopy(t), completed=True) for t in self.tasks.values()
            if t["checked"] and not t["is_deleted"] and t["completed_at"] and lo <= _parse_dt(t["completed_at"]) < hi])

    def find_by_marker(self, marker: str, project_id: str | None = None) -> RemoteTask | None:
        def fn() -> RemoteTask | None:
            for t in self.tasks.values():
                if t["checked"] or t["is_deleted"] or (project_id and t["project_id"] != project_id):
                    continue
                if marker in (t["description"] or "") or marker in (t["content"] or ""):
                    return parse_task(copy.deepcopy(t))
            return None
        return self._run("find_by_marker", (marker, project_id), fn)

    # -- writes
    def create_task(self, op_uuid: str, args: dict) -> RemoteTask:
        sync_args = build_sync_args(args)
        tid = self._command("create_task", op_uuid, (args,), lambda: self._make(sync_args)["id"])
        return parse_task(copy.deepcopy(self.tasks[tid]))

    def update_task(self, op_uuid: str, task_id: str, args: dict) -> RemoteTask:
        sync_args = build_sync_args(args, update=True)
        def fn() -> str:
            self._apply_args(self._live(task_id), sync_args)
            return task_id
        self._command("update_task", op_uuid, (task_id, args), fn)
        return parse_task(copy.deepcopy(self._live(task_id)))

    def close_task(self, op_uuid: str, task_id: str) -> None:
        def fn() -> str:
            raw = self._live(task_id)
            raw.update(checked=True, completed_at=_now_iso(), updated_at=_now_iso())
            return task_id
        self._command("close_task", op_uuid, (task_id,), fn)

    def reopen_task(self, op_uuid: str, task_id: str) -> None:
        def fn() -> str:
            raw = self._live(task_id)
            raw.update(checked=False, completed_at=None, updated_at=_now_iso())
            return task_id
        self._command("reopen_task", op_uuid, (task_id,), fn)

    def delete_task(self, op_uuid: str, task_id: str) -> None:
        def fn() -> str:
            raw = self.tasks.get(task_id)
            if raw is not None:  # deleting an already-deleted task is a no-op (adapter semantics)
                raw.update(is_deleted=True, updated_at=_now_iso())
            return task_id
        self._command("delete_task", op_uuid, (task_id,), fn)

    # -- simulate AJ acting in the Todoist app
    def external_create(self, content: str, description: str = "", project_id: str = "inbox", **args: Any) -> str:
        return self._make(build_sync_args({"content": content, "description": description,
                                           "project_id": project_id, **args}))["id"]

    def external_edit(self, task_id: str, **args: Any) -> None:
        self._apply_args(self._live(task_id), build_sync_args(args, update=True))

    def external_complete(self, task_id: str, completed_at: str | None = None) -> None:
        self._live(task_id).update(checked=True, completed_at=completed_at or _now_iso(), updated_at=_now_iso())

    def external_reopen(self, task_id: str) -> None:
        self._live(task_id).update(checked=False, completed_at=None, updated_at=_now_iso())

    def external_delete(self, task_id: str) -> None:
        self._live(task_id).update(is_deleted=True, updated_at=_now_iso())


# =========================================================================== Google Calendar

def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _bound(t: dict) -> datetime:
    if "dateTime" in t:
        return _parse_dt(t["dateTime"])
    d = date.fromisoformat(t["date"])
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGoogleCalendar(_FaultMixin):
    name = "google_calendar"
    simulated = True

    def __init__(self) -> None:
        self._init_faults()
        self._etag = itertools.count(1)
        self.calendars: dict[str, dict] = {}
        self.events: dict[tuple[str, str], dict] = {}
        self.add_calendar("primary", "AJ", access_role="owner", primary=True)

    def add_calendar(self, calendar_id: str, summary: str, *, access_role: str = "owner", primary: bool = False,
                     time_zone: str = "America/New_York") -> None:
        self.calendars[calendar_id] = {"id": calendar_id, "summary": summary, "accessRole": access_role,
                                       "primary": primary, "timeZone": time_zone}

    def _next_etag(self) -> str:
        return f'"{next(self._etag)}"'

    def _get_raw(self, calendar_id: str, event_id: str) -> dict | None:
        if calendar_id not in self.calendars:
            raise ProviderError("not_found", f"calendar {calendar_id} not found", status=404)
        return self.events.get((calendar_id, event_id))

    def _writable(self, calendar_id: str) -> None:
        cal = self.calendars.get(calendar_id)
        if cal is None:
            raise ProviderError("not_found", f"calendar {calendar_id} not found", status=404)
        if cal["accessRole"] not in ("owner", "writer"):
            raise ProviderError("forbidden", f"calendar {calendar_id} is read-only", status=403)

    def _ev(self, calendar_id: str, raw: dict) -> RemoteEvent:
        return parse_event(calendar_id, copy.deepcopy(raw))

    # -- reads
    def list_calendars(self) -> list[dict]:
        return self._run("list_calendars", (), lambda: copy.deepcopy(list(self.calendars.values())))

    def list_events(self, calendar_id: str, time_min: str, time_max: str) -> list[RemoteEvent]:
        lo, hi = _parse_dt(time_min), _parse_dt(time_max)

        def fn() -> list[RemoteEvent]:
            if calendar_id not in self.calendars:
                raise ProviderError("not_found", f"calendar {calendar_id} not found", status=404)
            rows = [r for (c, _), r in self.events.items() if c == calendar_id and r.get("status") != "cancelled"
                    and _bound(r["start"]) < hi and _bound(r["end"]) > lo]
            rows.sort(key=lambda r: _bound(r["start"]))
            return [self._ev(calendar_id, r) for r in rows]
        return self._run("list_events", (calendar_id, time_min, time_max), fn)

    def get_event(self, calendar_id: str, event_id: str) -> RemoteEvent | None:
        def fn() -> RemoteEvent | None:
            raw = self._get_raw(calendar_id, event_id)
            return None if raw is None else self._ev(calendar_id, raw)
        return self._run("get_event", (calendar_id, event_id), fn)

    def free_busy(self, calendar_ids: list[str], time_min: str, time_max: str) -> dict[str, list[dict]]:
        lo, hi = _parse_dt(time_min), _parse_dt(time_max)

        def fn() -> dict[str, list[dict]]:
            out: dict[str, list[dict]] = {}
            for cid in calendar_ids:
                if cid not in self.calendars:
                    raise ProviderError("not_found", f"freebusy error for {cid}: ['notFound']")
                spans = []
                for (c, _), r in self.events.items():
                    ev = self._ev(c, r)
                    if c != cid or not ev.busy:
                        continue
                    s, e = max(_bound(r["start"]), lo), min(_bound(r["end"]), hi)
                    if s < e:
                        spans.append([s, e])
                spans.sort()
                merged: list[list[datetime]] = []
                for s, e in spans:
                    if merged and s <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], e)
                    else:
                        merged.append([s, e])
                out[cid] = [{"start": _fmt(s), "end": _fmt(e)} for s, e in merged]
            return out
        return self._run("free_busy", (list(calendar_ids), time_min, time_max), fn)

    # -- writes
    def insert_event(self, calendar_id: str, event_id: str, body: dict) -> RemoteEvent:
        check_event_body(body)
        if not EVENT_ID_RE.match(event_id):
            raise ProviderError("validation", "event_id must be base32hex [a-v0-9], length 5-1024")

        def fn() -> RemoteEvent:
            self._writable(calendar_id)
            existing = self.events.get((calendar_id, event_id))
            if existing is not None:  # Google: 409 duplicate id -> adapter GETs and verifies ownership
                ours = ((body.get("extendedProperties") or {}).get("private") or {}).get(OP_PROP)
                theirs = ((existing.get("extendedProperties") or {}).get("private") or {}).get(OP_PROP)
                if ours and ours == theirs and existing.get("status") != "cancelled":
                    return self._ev(calendar_id, existing)
                raise ProviderError("conflict", f"event id {event_id} exists and is not this operation's event",
                                    status=409, detail={"existing_status": existing.get("status"),
                                                        "existing_op": theirs})
            raw = {"kind": "calendar#event", "status": "confirmed", "transparency": "opaque",
                   **copy.deepcopy(body), "id": event_id, "etag": self._next_etag(),
                   "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
                   "organizer": {"email": calendar_id, "self": True}, "updated": _now_iso()}
            self.events[(calendar_id, event_id)] = raw
            return self._ev(calendar_id, raw)
        return self._run("insert_event", (calendar_id, event_id, body), fn)

    def patch_event(self, calendar_id: str, event_id: str, body: dict, etag: str | None) -> RemoteEvent:
        check_event_body(body)

        def fn() -> RemoteEvent:
            self._writable(calendar_id)
            raw = self.events.get((calendar_id, event_id))
            if raw is None:
                raise ProviderError("not_found", f"event {event_id} not found", status=404)
            if etag and etag != raw["etag"]:
                raise ProviderError("precondition_failed", "etag mismatch", status=412)
            _merge_patch(raw, body)
            raw["etag"] = self._next_etag()
            raw["updated"] = _now_iso()
            return self._ev(calendar_id, raw)
        return self._run("patch_event", (calendar_id, event_id, body, etag), fn)

    def delete_event(self, calendar_id: str, event_id: str, etag: str | None) -> None:
        def fn() -> None:
            self._writable(calendar_id)
            raw = self.events.get((calendar_id, event_id))
            if raw is None or raw.get("status") == "cancelled":
                return  # 404/410 -> already gone
            if etag and etag != raw["etag"]:
                raise ProviderError("precondition_failed", "etag mismatch", status=412)
            raw.update(status="cancelled", etag=self._next_etag(), updated=_now_iso())
        return self._run("delete_event", (calendar_id, event_id, etag), fn)

    # -- simulate AJ / other clients acting on the calendar
    def add_event(self, calendar_id: str, raw: dict) -> RemoteEvent:
        """Seed an arbitrary event (fixed commitment, all-day deadline, recurring instance, ...)."""
        if calendar_id not in self.calendars:
            self.add_calendar(calendar_id, calendar_id)
        ev = {"status": "confirmed", "transparency": "opaque", **copy.deepcopy(raw), "etag": self._next_etag()}
        ev.setdefault("id", f"ext{len(self.events):05d}")
        self.events[(calendar_id, ev["id"])] = ev
        return self._ev(calendar_id, ev)

    def external_edit(self, calendar_id: str, event_id: str, **fields: Any) -> None:
        raw = self.events[(calendar_id, event_id)]
        _merge_patch(raw, fields)
        raw["etag"] = self._next_etag()

    def external_delete(self, calendar_id: str, event_id: str) -> None:
        self.events[(calendar_id, event_id)].update(status="cancelled", etag=self._next_etag())


def _merge_patch(raw: dict, body: dict) -> None:
    for k, v in body.items():
        if k == "extendedProperties" and isinstance(v, dict):
            ext = raw.setdefault("extendedProperties", {})
            for scope, props in v.items():
                ext.setdefault(scope, {}).update(props or {})
        else:
            raw[k] = copy.deepcopy(v)

