"""Provider adapter contracts shared by the executor, specialists and tests.

Adapters are thin, typed wrappers over real provider HTTP APIs. They never decide policy
(approval/scope/budget) — the executor does, before any write method is reachable.

Error model: every failure raises ProviderError with `kind` and `outcome_known`.
`outcome_known=False` means the request may have reached the provider (timeout / connection
reset after send / 5xx on a write) and the executor MUST reconcile before any retry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


class ProviderError(Exception):
    KINDS: ClassVar[frozenset[str]] = frozenset({"auth", "forbidden", "not_found", "conflict", "rate_limit", "validation", "server", "timeout",
             "network", "precondition_failed", "config", "unsupported"})

    def __init__(self, kind: str, message: str, *, outcome_known: bool = True, status: int | None = None,
                 retry_after: float | None = None, detail: Any = None):
        assert kind in self.KINDS, kind
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.outcome_known = outcome_known
        self.status = status
        self.retry_after = retry_after
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limit", "server", "timeout", "network"}


@dataclass
class RemoteTask:
    id: str
    content: str
    description: str = ""
    project_id: str | None = None
    due: dict | None = None            # {"date": "YYYY-MM-DD", "datetime": iso|None, "timezone": str|None, "string": str}
    duration: dict | None = None
    priority: int = 1
    labels: list[str] = field(default_factory=list)
    is_completed: bool = False
    is_deleted: bool = False
    url: str | None = None
    parent_id: str | None = None
    updated_at: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class RemoteEvent:
    id: str
    calendar_id: str
    summary: str
    start: dict                         # Google shape: {"dateTime": ..., "timeZone": ...} or {"date": ...}
    end: dict
    status: str = "confirmed"           # confirmed | tentative | cancelled
    transparency: str = "opaque"        # opaque (busy) | transparent (free)
    etag: str | None = None
    html_link: str | None = None
    recurring_event_id: str | None = None
    original_start: dict | None = None
    attendees: list[dict] = field(default_factory=list)
    organizer_self: bool = True
    description: str = ""
    private_props: dict = field(default_factory=dict)   # extendedProperties.private
    raw: dict = field(default_factory=dict)

    @property
    def all_day(self) -> bool:
        return "date" in self.start and "dateTime" not in self.start

    @property
    def busy(self) -> bool:
        return self.status != "cancelled" and self.transparency != "transparent"


class TaskProvider(Protocol):
    name: str  # "todoist"

    def whoami(self) -> dict: ...
    def list_projects(self) -> list[dict]: ...
    def list_tasks(self, project_id: str | None = None) -> list[RemoteTask]: ...
    def get_task(self, task_id: str) -> RemoteTask | None: ...          # None if deleted / not found
    def list_completed(self, since_iso: str, until_iso: str) -> list[RemoteTask]: ...
    # Writes. `op_uuid` is a stable per-operation UUID used as the provider idempotency key
    # (Todoist Sync API command uuid). Re-sending the same op_uuid must not duplicate.
    def create_task(self, op_uuid: str, args: dict) -> RemoteTask: ...
    def update_task(self, op_uuid: str, task_id: str, args: dict) -> RemoteTask: ...
    def close_task(self, op_uuid: str, task_id: str) -> None: ...
    def reopen_task(self, op_uuid: str, task_id: str) -> None: ...
    def delete_task(self, op_uuid: str, task_id: str) -> None: ...
    def find_by_marker(self, marker: str, project_id: str | None = None) -> RemoteTask | None: ...


class CalendarProvider(Protocol):
    name: str  # "google_calendar"

    def list_calendars(self) -> list[dict]: ...          # [{id, summary, accessRole, primary, timeZone}]
    def list_events(self, calendar_id: str, time_min: str, time_max: str) -> list[RemoteEvent]: ...  # recurring expanded
    def get_event(self, calendar_id: str, event_id: str) -> RemoteEvent | None: ...
    def free_busy(self, calendar_ids: list[str], time_min: str, time_max: str) -> dict[str, list[dict]]: ...
    # Writes: event_id is client-chosen and deterministic from op_key (Google accepts client ids;
    # a 409 on insert means it already exists -> treat as already created and verify by GET).
    def insert_event(self, calendar_id: str, event_id: str, body: dict) -> RemoteEvent: ...
    def patch_event(self, calendar_id: str, event_id: str, body: dict, etag: str | None) -> RemoteEvent: ...
    def delete_event(self, calendar_id: str, event_id: str, etag: str | None) -> None: ...


class DriveProvider(Protocol):
    name: str  # "google_drive"

    def get_file(self, file_id: str) -> dict: ...                    # metadata incl. modifiedTime, md5Checksum, mimeType, parents
    def list_folder(self, folder_id: str) -> list[dict]: ...
    def search(self, query: str, folder_ids: list[str]) -> list[dict]: ...
    def download(self, file_id: str, mime_type: str, max_bytes: int) -> tuple[bytes, str]: ...  # (bytes, effective mime); Google Docs exported


class RepoStatusProvider(Protocol):
    name: str  # "github"

    def repo_status(self, full_name: str) -> dict: ...   # {repo, open_issues:[...], open_prs:[...], default_branch, pushed_at, url}


@dataclass
class MailMessage:
    id: str
    thread_id: str
    subject: str
    sender: str                         # "Name <addr>" as the provider reports it
    sender_email: str
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    date: str | None = None             # UTC ISO instant
    snippet: str = ""
    body_text: str | None = None        # only on get_message/get_thread (plain text; HTML is stripped)
    unread: bool = False
    important: bool = False
    labels: list[str] = field(default_factory=list)
    message_id_header: str | None = None   # RFC 5322 Message-ID, for In-Reply-To/References on replies
    web_url: str | None = None


@dataclass
class MailDraft:
    id: str
    message_id: str
    thread_id: str | None
    to: list[str]
    subject: str
    body_text: str
    op_key: str | None = None           # X-Jarvis-Op header / extended property used for reconciliation
    web_url: str | None = None


class MailProvider(Protocol):
    """Gmail / Outlook. Drafting never contacts anyone; `send_draft` exists only behind a T2 action AJ enabled."""
    name: str  # "gmail" | "outlook"

    def whoami(self) -> dict: ...                                       # {email, name}
    def list_messages(self, query: str | None, max_results: int, unread_only: bool = False) -> list[MailMessage]: ...
    def get_message(self, message_id: str) -> MailMessage | None: ...
    def get_thread(self, thread_id: str) -> list[MailMessage]: ...
    # Writes. Drafts carry `op_key` in a hidden header/property (never in the visible body) for reconciliation.
    def create_draft(self, op_key: str, to: list[str], subject: str, body: str, *, cc: list[str] | None = None,
                     reply_to: MailMessage | None = None) -> MailDraft: ...
    def get_draft(self, draft_id: str) -> MailDraft | None: ...
    def find_draft_by_op(self, op_key: str) -> MailDraft | None: ...
    def delete_draft(self, draft_id: str) -> None: ...
    def send_draft(self, draft_id: str) -> str: ...                     # returns the sent message id
