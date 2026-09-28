"""Todoist adapter (TaskProvider) for the current Todoist API v1.

Verified against https://developer.todoist.com/api/v1/ (embedded OpenAPI spec, 2026-09-28):

* Base URL ``https://api.todoist.com/api/v1``, ``Authorization: Bearer <token>``.
* Paginated list endpoints (``GET /tasks``, ``GET /projects``) return
  ``{"results": [...], "next_cursor": str | null}``; ``limit`` max 200, ``cursor`` param.
* Completed tasks: ``GET /tasks/completed/by_completion_date?since=&until=`` (range up to 3 months,
  ``until`` exclusive) returns ``{"items": [...], "next_cursor": ...}`` -- key is ``items``.
* ``GET /tasks/{id}`` returns a single task object (fields: ``checked``, ``is_deleted``,
  ``completed_at``, ``updated_at``; there is no ``url`` field, so we build
  ``https://app.todoist.com/app/task/{id}``).
* Due objects carry only ``date``: ``YYYY-MM-DD`` (full day), ``YYYY-MM-DDTHH:MM:SS`` (floating) or
  ``YYYY-MM-DDTHH:MM:SSZ`` + ``timezone`` (fixed). We normalise to the RemoteTask due shape.
* Writes go through ``POST /sync`` (form-encoded ``commands`` JSON). Command types: ``item_add``
  (with ``temp_id``), ``item_update`` (cannot move/complete), ``item_close`` (what clients do:
  completes a regular task, advances a recurring one), ``item_uncomplete``, ``item_delete``.
  "Todoist will not execute a command that has same UUID as a previously executed command" --
  so ``op_uuid`` is used as the command ``uuid`` and is the provider idempotency key.
* ``sync_status[uuid]`` is ``"ok"`` or ``{error_tag, error_code, error, http_code, error_extra}``.
  ``error_extra.retry_after`` may appear on any error.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from .base import ProviderError, RemoteTask

API_BASE = "https://api.todoist.com/api/v1"
TASK_URL = "https://app.todoist.com/app/task/{id}"
PAGE_LIMIT = 200
MARKER_RE = re.compile(r"\[jarvis:op=[^\]\s]+\]")

# Keys accepted in the `args` dict for create/update (REST-style names, translated to Sync args).
_CREATE_KEYS = {"content", "description", "project_id", "section_id", "parent_id", "priority", "labels",
                "due", "due_string", "due_date", "due_datetime", "due_lang", "due_timezone",
                "duration", "duration_unit", "deadline_date"}
_UPDATE_KEYS = _CREATE_KEYS - {"project_id", "section_id", "parent_id"}


# --------------------------------------------------------------------------- HTTP error mapping

def _retry_after(resp: httpx.Response, body: Any = None) -> float | None:
    val = resp.headers.get("Retry-After")
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    if isinstance(body, dict):
        extra = body.get("error_extra") or {}
        if isinstance(extra, dict) and extra.get("retry_after") is not None:
            try:
                return float(extra["retry_after"])
            except (TypeError, ValueError):
                return None
    return None


def _status_error(resp: httpx.Response, *, write: bool) -> ProviderError:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    code = resp.status_code
    msg = body.get("error") if isinstance(body, dict) and body.get("error") else f"HTTP {code}"
    kw: dict[str, Any] = {"status": code, "detail": body}
    if code == 401:
        return ProviderError("auth", f"Todoist auth failed (reconnect/replace token): {msg}", **kw)
    if code == 403:
        return ProviderError("forbidden", f"Todoist forbidden: {msg}", **kw)
    if code in (404, 410):
        return ProviderError("not_found", f"Todoist not found: {msg}", **kw)
    if code == 409:
        return ProviderError("conflict", f"Todoist conflict: {msg}", **kw)
    if code == 429:
        return ProviderError("rate_limit", "Todoist rate limit", retry_after=_retry_after(resp, body), **kw)
    if code >= 500:
        return ProviderError("server", f"Todoist server error {code}", outcome_known=not write,
                             retry_after=_retry_after(resp, body), **kw)
    return ProviderError("validation", f"Todoist rejected request: {msg}", **kw)


def _transport_error(exc: httpx.TransportError, *, write: bool) -> ProviderError:
    # Connect failures happen before the request is sent: the outcome is known (nothing happened).
    if isinstance(exc, httpx.ConnectTimeout):
        return ProviderError("timeout", f"connect timeout: {exc}", outcome_known=True)
    if isinstance(exc, httpx.ConnectError):
        return ProviderError("network", f"connect error: {exc}", outcome_known=True)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"timeout: {exc}", outcome_known=not write)
    return ProviderError("network", f"network error: {exc}", outcome_known=not write)


# --------------------------------------------------------------------------- parsing / arg building

def normalize_due(due: dict | None) -> dict | None:
    """Todoist v1 due -> RemoteTask shape {date, datetime, timezone, string, lang, is_recurring}."""
    if not due:
        return None
    raw_date = due.get("date")
    date_part, dt_part = raw_date, None
    if isinstance(raw_date, str) and "T" in raw_date:
        date_part, dt_part = raw_date[:10], raw_date
    return {"date": date_part, "datetime": dt_part, "timezone": due.get("timezone"),
            "string": due.get("string"), "lang": due.get("lang"),
            "is_recurring": bool(due.get("is_recurring", False))}


def parse_task(d: dict, *, completed: bool | None = None) -> RemoteTask:
    tid = str(d["id"])
    return RemoteTask(
        id=tid,
        content=d.get("content") or "",
        description=d.get("description") or "",
        project_id=str(d["project_id"]) if d.get("project_id") is not None else None,
        due=normalize_due(d.get("due")),
        duration=d.get("duration"),
        priority=int(d.get("priority") or 1),
        labels=list(d.get("labels") or []),
        is_completed=bool(d.get("checked")) if completed is None else completed,
        is_deleted=bool(d.get("is_deleted", False)),
        url=d.get("url") or TASK_URL.format(id=tid),
        parent_id=str(d["parent_id"]) if d.get("parent_id") is not None else None,
        updated_at=d.get("updated_at"),
        raw=d,
    )


def _datetime_arg(value: str) -> str:
    """Floating 'YYYY-MM-DDTHH:MM[:SS]' passes through; an aware datetime becomes fixed UTC '...Z'."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderError("validation", f"invalid due_datetime {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_sync_args(args: dict, *, update: bool = False) -> dict:
    """Translate adapter args (REST-style names) into Sync ``item_add``/``item_update`` args.

    Raises ProviderError('validation') for unknown keys or contradictory due fields and
    ProviderError('unsupported') for moves on update (needs item_move; not offered).
    """
    allowed = _UPDATE_KEYS if update else _CREATE_KEYS
    unknown = set(args) - allowed
    if update and unknown & {"project_id", "section_id", "parent_id"}:
        raise ProviderError("unsupported", "moving a task (project/section/parent) is not supported by update")
    if unknown:
        raise ProviderError("validation", f"unsupported task fields: {sorted(unknown)}")
    if not update and not (args.get("content") or "").strip():
        raise ProviderError("validation", "content is required")

    out: dict[str, Any] = {}
    for key in ("content", "description", "project_id", "section_id", "parent_id", "labels"):
        if key in args:
            out[key] = args[key]
    if "priority" in args:
        pr = int(args["priority"])
        if pr not in (1, 2, 3, 4):
            raise ProviderError("validation", "priority must be 1..4 (4 = most urgent / p1 in clients)")
        out["priority"] = pr

    due_keys = [k for k in ("due", "due_string", "due_date", "due_datetime") if k in args]
    if len(due_keys) > 1:
        raise ProviderError("validation", f"specify only one of {due_keys}")
    if due_keys:
        key = due_keys[0]
        val = args[key]
        if val is None:
            out["due"] = None
        elif key == "due":
            out["due"] = dict(val)
        elif key == "due_string":
            out["due"] = {"string": val}
        elif key == "due_date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(val)):
                raise ProviderError("validation", f"due_date must be YYYY-MM-DD, got {val!r}")
            out["due"] = {"date": val}
        else:
            out["due"] = {"date": _datetime_arg(str(val))}
        if out["due"] is not None:
            if args.get("due_lang"):
                out["due"]["lang"] = args["due_lang"]
            if args.get("due_timezone"):
                out["due"]["timezone"] = args["due_timezone"]
    elif args.get("due_lang") or args.get("due_timezone"):
        raise ProviderError("validation", "due_lang/due_timezone require a due value")

    if "duration" in args:
        dur = args["duration"]
        if dur is None:
            out["duration"] = None
        elif isinstance(dur, dict):
            out["duration"] = {"amount": int(dur["amount"]), "unit": dur["unit"]}
        else:
            out["duration"] = {"amount": int(dur), "unit": args.get("duration_unit") or "minute"}
        if out["duration"] is not None and (
                out["duration"]["amount"] <= 0 or out["duration"]["unit"] not in ("minute", "day")):
            raise ProviderError("validation", "duration amount must be > 0 and unit minute|day")
    elif "duration_unit" in args:
        raise ProviderError("validation", "duration_unit requires duration")

    if "deadline_date" in args:
        out["deadline"] = None if args["deadline_date"] is None else {"date": args["deadline_date"]}
    return out


def _sync_error(uuid: str, status: dict) -> ProviderError:
    http_code = status.get("http_code")
    tag = status.get("error_tag")
    msg = f"Todoist command failed: {status.get('error') or tag or 'unknown error'} ({tag})"
    detail = {"uuid": uuid, **status}
    extra = status.get("error_extra") or {}
    if http_code == 401:
        return ProviderError("auth", msg, status=401, detail=detail)
    if http_code == 403:
        return ProviderError("forbidden", msg, status=403, detail=detail)
    if http_code == 404 or (tag or "").endswith("NOT_FOUND"):
        return ProviderError("not_found", msg, status=http_code, detail=detail)
    if http_code == 429:
        return ProviderError("rate_limit", msg, status=429, retry_after=extra.get("retry_after"), detail=detail)
    if http_code and http_code >= 500:
        return ProviderError("server", msg, status=http_code, outcome_known=False, detail=detail)
    return ProviderError("validation", msg, status=http_code, detail=detail)


# --------------------------------------------------------------------------- adapter

class TodoistAdapter:
    """TaskProvider over Todoist API v1. Reads via REST, writes via Sync commands keyed by op_uuid."""

    name = "todoist"
    simulated = False

    def __init__(self, token: str, client: httpx.Client | None = None, *, base_url: str = API_BASE,
                 timeout: float = 15.0):
        if not token:
            raise ProviderError("config", "Todoist token is not configured")
        self._token = token
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def __repr__(self) -> str:  # never leak the token
        return "TodoistAdapter(token=***)"

    # -- transport
    def _request(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None,
                 write: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            resp = self._client.request(method, f"{self._base}{path}", params=params, data=data, headers=headers)
        except httpx.TransportError as exc:
            raise _transport_error(exc, write=write) from exc
        if resp.status_code >= 400:
            raise _status_error(resp, write=write)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError("server", "Todoist returned non-JSON", status=resp.status_code,
                                outcome_known=not write) from exc

    def _paginate(self, path: str, params: dict, key: str = "results") -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(1000):  # hard stop against a server cursor loop
            page_params = {k: v for k, v in {**params, "cursor": cursor}.items() if v is not None}
            body = self._request("GET", path, params=page_params) or {}
            out.extend(body.get(key) or [])
            cursor = body.get("next_cursor")
            if not cursor:
                return out
        raise ProviderError("server", f"pagination did not terminate for {path}")

    def _sync(self, commands: list[dict]) -> dict:
        body = self._request("POST", "/sync", data={"commands": json.dumps(commands)}, write=True) or {}
        statuses = body.get("sync_status") or {}
        for cmd in commands:
            status = statuses.get(cmd["uuid"])
            if status == "ok":
                continue
            if isinstance(status, dict):
                raise _sync_error(cmd["uuid"], status)
            raise ProviderError("server", f"no sync_status for command {cmd['uuid']}", outcome_known=False,
                                detail=body)
        return body

    # -- reads
    def whoami(self) -> dict:
        u = self._request("GET", "/user") or {}
        return {"id": str(u.get("id")), "email": u.get("email"), "full_name": u.get("full_name"),
                "timezone": (u.get("tz_info") or {}).get("timezone"), "inbox_project_id": u.get("inbox_project_id")}

    def list_projects(self) -> list[dict]:
        return self._paginate("/projects", {"limit": PAGE_LIMIT})

    def list_tasks(self, project_id: str | None = None) -> list[RemoteTask]:
        rows = self._paginate("/tasks", {"limit": PAGE_LIMIT, "project_id": project_id})
        return [parse_task(r) for r in rows]

    def get_task(self, task_id: str) -> RemoteTask | None:
        try:
            body = self._request("GET", f"/tasks/{task_id}")
        except ProviderError as exc:
            if exc.kind == "not_found":
                return None
            raise
        if not body or body.get("is_deleted"):
            return None
        return parse_task(body)

    def list_completed(self, since_iso: str, until_iso: str) -> list[RemoteTask]:
        rows = self._paginate("/tasks/completed/by_completion_date",
                              {"since": since_iso, "until": until_iso, "limit": PAGE_LIMIT}, key="items")
        return [parse_task(r, completed=True) for r in rows]

    def find_by_marker(self, marker: str, project_id: str | None = None) -> RemoteTask | None:
        if not marker:
            raise ProviderError("validation", "marker must be non-empty")
        for task in self.list_tasks(project_id):
            if marker in task.description or marker in task.content:
                return task
        return None

    # -- writes
    def create_task(self, op_uuid: str, args: dict) -> RemoteTask:
        sync_args = build_sync_args(args)
        body = self._sync([{"type": "item_add", "uuid": op_uuid, "temp_id": op_uuid, "args": sync_args}])
        real_id = (body.get("temp_id_mapping") or {}).get(op_uuid)
        if real_id is None:
            # A replayed uuid is not re-executed and may not echo the mapping; recover via the marker.
            m = MARKER_RE.search(sync_args.get("description") or "")
            found = self.find_by_marker(m.group(0), sync_args.get("project_id")) if m else None
            if found:
                return found
            raise ProviderError("server", "item_add ok but no temp_id_mapping; reconcile required",
                                outcome_known=False, detail=body)
        task = self.get_task(str(real_id))
        if task is None:
            raise ProviderError("not_found", f"created task {real_id} not readable", outcome_known=True)
        return task

    def update_task(self, op_uuid: str, task_id: str, args: dict) -> RemoteTask:
        sync_args = build_sync_args(args, update=True)
        if not sync_args:
            raise ProviderError("validation", "no fields to update")
        self._sync([{"type": "item_update", "uuid": op_uuid, "args": {"id": task_id, **sync_args}}])
        task = self.get_task(task_id)
        if task is None:
            raise ProviderError("not_found", f"task {task_id} not found after update")
        return task

    def close_task(self, op_uuid: str, task_id: str) -> None:
        self._sync([{"type": "item_close", "uuid": op_uuid, "args": {"id": task_id}}])

    def reopen_task(self, op_uuid: str, task_id: str) -> None:
        self._sync([{"type": "item_uncomplete", "uuid": op_uuid, "args": {"id": task_id}}])

    def delete_task(self, op_uuid: str, task_id: str) -> None:
        try:
            self._sync([{"type": "item_delete", "uuid": op_uuid, "args": {"id": task_id}}])
        except ProviderError as exc:
            if exc.kind != "not_found":
                raise
            # Already gone: delete is idempotent from the executor's point of view.
