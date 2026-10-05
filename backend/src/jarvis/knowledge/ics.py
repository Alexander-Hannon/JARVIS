"""ICS deadline-feed parsing, course classification, and idempotent upsert into deadline_items.

This module never fetches anything and never sees the feed URL (a secret handled elsewhere).
Feed text (summaries, descriptions) is untrusted data and is stored, never interpreted.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from jarvis.core.db import row, rows
from jarvis.core.util import canonical_hash, iso, new_id, now_iso


def _to_utc(dt: datetime, default_tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:  # floating time -> user's timezone
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


def _tz_name(dt: date | datetime, default_tz: str) -> str:
    if not isinstance(dt, datetime):
        return default_tz
    if dt.tzinfo is None:
        return default_tz
    key = getattr(dt.tzinfo, "key", None) or getattr(dt.tzinfo, "zone", None)
    return str(key or dt.tzname() or "UTC")


def _recurring_uids(cal) -> set[str]:
    out = set()
    for comp in cal.walk("VEVENT"):
        if comp.get("RRULE") is not None or comp.get("RDATE") is not None or comp.get("RECURRENCE-ID") is not None:
            out.add(str(comp.get("UID", "")))
    return out


def _ident(v: date | datetime, tz: ZoneInfo) -> str:
    if isinstance(v, datetime):
        return iso(_to_utc(v, tz))
    return v.isoformat()


def calendar_key(summary: str, local_date: str | None) -> str:
    """Key used to detect a feed entry already present in Google Calendar (avoid duplicate reminders)."""
    return f"{(summary or '').strip().lower()}|{local_date or ''}"


def parse_feed(ics_bytes: bytes, window_start: date, window_end: date, default_tz: str = "America/Chicago") -> list[dict]:
    """Expand VEVENTs occurring in [window_start, window_end). Cancelled occurrences are returned with
    status 'cancelled'; EXDATE'd occurrences are simply absent. local_date is in default_tz (the user's zone)."""
    import icalendar
    import recurring_ical_events

    tz = ZoneInfo(default_tz)
    cal = icalendar.Calendar.from_ical(ics_bytes)
    recurring = _recurring_uids(cal)
    items: dict[tuple[str, str], dict] = {}
    for ev in recurring_ical_events.of(cal, components=["VEVENT"]).between(window_start, window_end):
        uid = str(ev.get("UID", "")).strip()
        if not uid:
            continue
        ds = ev.get("DTSTART")
        if ds is None:
            continue
        start = ds.dt
        de = ev.get("DTEND")
        end = de.dt if de is not None else None
        all_day = not isinstance(start, datetime)
        rid = ""
        if uid in recurring:
            r = ev.get("RECURRENCE-ID")
            rid = _ident(r.dt if r is not None else start, tz)
        if all_day:
            start_at = end_at = None
            local_date = start.isoformat()
        else:
            su = _to_utc(start, tz)
            start_at = iso(su)
            end_at = iso(_to_utc(end, tz)) if isinstance(end, datetime) else None
            local_date = su.astimezone(tz).date().isoformat()
        status = str(ev.get("STATUS", "CONFIRMED") or "CONFIRMED").lower()
        if status not in ("confirmed", "tentative", "cancelled"):
            status = "confirmed"
        try:
            sequence = int(ev.get("SEQUENCE", 0) or 0)
        except (TypeError, ValueError):
            sequence = 0
        item = {
            "uid": uid, "recurrence_id": rid,
            "summary": str(ev.get("SUMMARY", "") or "").strip(),
            "description": str(ev.get("DESCRIPTION")) if ev.get("DESCRIPTION") is not None else None,
            "url": str(ev.get("URL")) if ev.get("URL") is not None else None,
            "start_at": start_at, "end_at": end_at, "all_day": all_day, "local_date": local_date,
            "tz": _tz_name(start, default_tz), "status": status, "sequence": sequence,
        }
        item["content_hash"] = canonical_hash({k: item[k] for k in (
            "summary", "description", "url", "start_at", "end_at", "all_day", "local_date", "status")})
        key = (uid, rid)
        prev = items.get(key)
        if prev is None or sequence >= prev["sequence"]:
            items[key] = item
    return sorted(items.values(), key=lambda i: (i["local_date"] or "", i["start_at"] or "", i["uid"]))


def classify(summary: str, mappings: Iterable[dict]) -> tuple[str | None, str]:
    """Case-insensitive substring match. Any disagreement (roles or courses) -> (None, 'unknown')."""
    s = (summary or "").lower()
    hits = {(m["course"], m["role"]) for m in mappings
            if m.get("pattern") and m["pattern"].strip().lower() in s and m.get("role") in ("own", "gta")}
    if len(hits) != 1:
        return None, "unknown"
    course, role = next(iter(hits))
    return course, role


def upsert_deadlines(conn: sqlite3.Connection, account_id: str, items: list[dict],
                     calendar_event_keys: set[str] | frozenset[str] = frozenset(), *,
                     window_start: date | None = None, window_end: date | None = None,
                     confirmed_mappings_only: bool = True) -> dict:
    """Idempotent upsert keyed by (account_id, uid, recurrence_id).

    Rows missing from `items` are marked cancelled only if window_start/window_end are given and
    the row's local_date is inside [window_start, window_end). A stale item (lower SEQUENCE than
    stored) does not overwrite newer data.
    """
    mappings = rows(conn, "SELECT pattern, course, role FROM course_mappings" +  # nosec B608 fixed clause
                    (" WHERE confirmed = 1" if confirmed_mappings_only else ""))
    counts = {"added": 0, "updated": 0, "cancelled": 0, "unchanged": 0}
    now = now_iso()
    seen: set[tuple[str, str]] = set()
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for it in items:
            key = (it["uid"], it.get("recurrence_id") or "")
            seen.add(key)
            course, role = classify(it["summary"], mappings)
            also = 1 if calendar_key(it["summary"], it.get("local_date")) in calendar_event_keys else 0
            vals = {
                "summary": it["summary"], "description": it.get("description"), "url": it.get("url"),
                "start_at": it.get("start_at"), "end_at": it.get("end_at"), "all_day": 1 if it.get("all_day") else 0,
                "local_date": it.get("local_date"), "tz": it.get("tz"), "status": it.get("status", "confirmed"),
                "sequence": int(it.get("sequence") or 0), "course": course, "role": role,
                "also_in_calendar": also, "content_hash": it["content_hash"],
            }
            ex = row(conn, "SELECT * FROM deadline_items WHERE account_id=? AND uid=? AND recurrence_id=?",
                     (account_id, key[0], key[1]))
            if ex is None:
                cols = ["id", "account_id", "uid", "recurrence_id", *vals, "first_seen_at", "last_seen_at"]
                conn.execute(f"INSERT INTO deadline_items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # nosec B608 columns from code; values bound
                             [new_id("dl"), account_id, key[0], key[1], *vals.values(), now, now])
                counts["cancelled" if vals["status"] == "cancelled" else "added"] += 1
                continue
            if vals["sequence"] < ex["sequence"]:
                conn.execute("UPDATE deadline_items SET last_seen_at=? WHERE id=?", (now, ex["id"]))
                counts["unchanged"] += 1
                continue
            changed = any(ex[k] != v for k, v in vals.items())
            if changed:
                sets = ", ".join(f"{k}=?" for k in vals)
                conn.execute(f"UPDATE deadline_items SET {sets}, last_seen_at=? WHERE id=?",  # nosec B608 columns from code; values bound
                             [*vals.values(), now, ex["id"]])
                if vals["status"] == "cancelled" and ex["status"] != "cancelled":
                    counts["cancelled"] += 1
                else:
                    counts["updated"] += 1
            else:
                conn.execute("UPDATE deadline_items SET last_seen_at=? WHERE id=?", (now, ex["id"]))
                counts["unchanged"] += 1
        if window_start is not None and window_end is not None:
            for ex in rows(conn, "SELECT id, uid, recurrence_id FROM deadline_items WHERE account_id=? "
                                 "AND status != 'cancelled' AND local_date >= ? AND local_date < ?",
                           (account_id, window_start.isoformat(), window_end.isoformat())):
                if (ex["uid"], ex["recurrence_id"]) not in seen:
                    conn.execute("UPDATE deadline_items SET status='cancelled' WHERE id=?", (ex["id"],))
                    counts["cancelled"] += 1
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    return counts
