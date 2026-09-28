"""Google Calendar adapter (CalendarProvider) over https://www.googleapis.com/calendar/v3.

Verified 2026-09-28 against https://developers.google.com/workspace/calendar/api/v3/reference:
* events.insert accepts a client-supplied ``id``: "characters allowed in the ID are those used in
  base32hex encoding, i.e. lowercase letters a-v and digits 0-9", length 5-1024, unique per calendar.
  Re-inserting an existing id yields 409 -> we GET it and return it only if its private extended
  property ``jarvis_op`` matches ours (idempotent create); otherwise ``conflict``.
* ``sendUpdates`` in {all, externalOnly, none}; we always send ``none``. Attendees are refused.
* freebusy.query body {timeMin, timeMax, items:[{id}]} -> {calendars: {id: {busy:[{start,end}], errors:[...]}}}.
* events.patch/delete honour ``If-Match: <etag>`` (412 on mismatch). Deleted events remain
  retrievable with status "cancelled"; delete of an already-deleted event returns 410.
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any
from urllib.parse import quote

from .base import ProviderError, RemoteEvent
from .google_oauth import GoogleCredentials

API_BASE = "https://www.googleapis.com/calendar/v3"
OP_PROP = "jarvis_op"
EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")
_FORBIDDEN_BODY_KEYS = ("attendees", "organizer", "attendeesOmitted")


def event_id_from_op_key(op_key: str) -> str:
    """Deterministic Google-valid event id (base32hex, lowercase, 54 chars) for an operation key."""
    if not op_key:
        raise ValueError("op_key required")
    digest = hashlib.sha256(op_key.encode("utf-8")).digest()
    return "jv" + base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


def check_event_body(body: dict) -> None:
    """Refuse bodies that would invite people or change organizers (unsupported in this release)."""
    for key in _FORBIDDEN_BODY_KEYS:
        if body.get(key):
            raise ProviderError("unsupported", f"event field {key!r} is not supported (no attendees/invitations)")


def parse_event(calendar_id: str, d: dict) -> RemoteEvent:
    ext = (d.get("extendedProperties") or {}).get("private") or {}
    organizer = d.get("organizer")
    return RemoteEvent(
        id=d["id"],
        calendar_id=calendar_id,
        summary=d.get("summary") or "",
        start=dict(d.get("start") or {}),
        end=dict(d.get("end") or {}),
        status=d.get("status") or "confirmed",
        transparency=d.get("transparency") or "opaque",
        etag=d.get("etag"),
        html_link=d.get("htmlLink"),
        recurring_event_id=d.get("recurringEventId"),
        original_start=d.get("originalStartTime"),
        attendees=list(d.get("attendees") or []),
        organizer_self=bool(organizer.get("self", False)) if isinstance(organizer, dict) else True,
        description=d.get("description") or "",
        private_props=dict(ext),
        raw=d,
    )


def _cal(calendar_id: str) -> str:
    return quote(calendar_id, safe="")


class GoogleCalendarAdapter:
    name = "google_calendar"
    simulated = False

    def __init__(self, credentials: GoogleCredentials, *, base_url: str = API_BASE):
        self._creds = credentials
        self._base = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    # -- reads
    def list_calendars(self) -> list[dict]:
        out: list[dict] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"maxResults": 250}
            if token:
                params["pageToken"] = token
            body = self._creds.request("GET", self._url("/users/me/calendarList"), params=params).json()
            for c in body.get("items") or []:
                out.append({"id": c["id"], "summary": c.get("summaryOverride") or c.get("summary") or c["id"],
                            "accessRole": c.get("accessRole"), "primary": bool(c.get("primary", False)),
                            "timeZone": c.get("timeZone")})
            token = body.get("nextPageToken")
            if not token:
                return out

    def list_events(self, calendar_id: str, time_min: str, time_max: str) -> list[RemoteEvent]:
        out: list[RemoteEvent] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"singleEvents": "true", "orderBy": "startTime", "showDeleted": "false",
                                      "timeMin": time_min, "timeMax": time_max, "maxResults": 2500}
            if token:
                params["pageToken"] = token
            body = self._creds.request("GET", self._url(f"/calendars/{_cal(calendar_id)}/events"),
                                       params=params).json()
            out.extend(parse_event(calendar_id, e) for e in body.get("items") or [])
            token = body.get("nextPageToken")
            if not token:
                return out

    def get_event(self, calendar_id: str, event_id: str) -> RemoteEvent | None:
        try:
            resp = self._creds.request("GET", self._url(f"/calendars/{_cal(calendar_id)}/events/{quote(event_id)}"))
        except ProviderError as exc:
            if exc.kind == "not_found":
                return None
            raise
        return parse_event(calendar_id, resp.json())

    def free_busy(self, calendar_ids: list[str], time_min: str, time_max: str) -> dict[str, list[dict]]:
        body = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": c} for c in calendar_ids]}
        data = self._creds.request("POST", self._url("/freeBusy"), json=body).json()
        cals = data.get("calendars") or {}
        out: dict[str, list[dict]] = {}
        for cid in calendar_ids:
            entry = cals.get(cid)
            if entry is None:
                raise ProviderError("server", f"freebusy returned no entry for {cid}", detail=data)
            if entry.get("errors"):
                reasons = [e.get("reason") for e in entry["errors"]]
                kind = "not_found" if "notFound" in reasons else "server"
                # Missing availability is not "free": surface it instead of returning [].
                raise ProviderError(kind, f"freebusy error for {cid}: {reasons}", detail={"calendar": cid,
                                                                                         "errors": entry["errors"]})
            out[cid] = [{"start": b["start"], "end": b["end"]} for b in entry.get("busy") or []]
        return out

    # -- writes
    def insert_event(self, calendar_id: str, event_id: str, body: dict) -> RemoteEvent:
        check_event_body(body)
        if not EVENT_ID_RE.match(event_id):
            raise ProviderError("validation", "event_id must be base32hex [a-v0-9], length 5-1024")
        payload = {**body, "id": event_id}
        our_op = ((body.get("extendedProperties") or {}).get("private") or {}).get(OP_PROP)
        try:
            resp = self._creds.request("POST", self._url(f"/calendars/{_cal(calendar_id)}/events"),
                                       params={"sendUpdates": "none"}, json=payload, write=True)
        except ProviderError as exc:
            if exc.kind != "conflict":
                raise
            existing = self.get_event(calendar_id, event_id)
            if existing is None:
                raise
            if our_op and existing.private_props.get(OP_PROP) == our_op and existing.status != "cancelled":
                return existing  # our earlier insert landed; idempotent success
            raise ProviderError("conflict", f"event id {event_id} exists and is not this operation's event",
                                status=409, detail={"existing_status": existing.status,
                                                    "existing_op": existing.private_props.get(OP_PROP)}) from exc
        return parse_event(calendar_id, resp.json())

    def patch_event(self, calendar_id: str, event_id: str, body: dict, etag: str | None) -> RemoteEvent:
        check_event_body(body)
        headers = {"If-Match": etag} if etag else {}
        resp = self._creds.request("PATCH", self._url(f"/calendars/{_cal(calendar_id)}/events/{quote(event_id)}"),
                                   params={"sendUpdates": "none"}, json=body, headers=headers, write=True)
        return parse_event(calendar_id, resp.json())

    def delete_event(self, calendar_id: str, event_id: str, etag: str | None) -> None:
        headers = {"If-Match": etag} if etag else {}
        try:
            self._creds.request("DELETE", self._url(f"/calendars/{_cal(calendar_id)}/events/{quote(event_id)}"),
                                params={"sendUpdates": "none"}, headers=headers, write=True)
        except ProviderError as exc:
            if exc.kind == "not_found":  # 404 / 410 Gone: already deleted
                return
            raise
