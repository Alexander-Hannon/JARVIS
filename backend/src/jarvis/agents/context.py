"""Current-context snapshot shared by Today, Chief, specialists and briefings.

Every source reports its own status (ok / stale / unavailable / not_connected) so callers can say
"calendar not connected" instead of "nothing scheduled". Missing data is never rendered as "all clear".
Everything is per user: functions take `uid`, and every cache key includes it.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.core.db import jload, row, rows
from jarvis.core.util import iso, now_iso, parse_iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError

_cache: dict[str, tuple[float, object]] = {}
CACHE_S = 60


def user_tz(conn, uid: str) -> str:
    from jarvis.core import settings as S
    return S.user_tz(conn, uid)


def local_today(conn, uid: str) -> date:
    return utcnow().astimezone(ZoneInfo(user_tz(conn, uid))).date()


def day_bounds(d: date, tz: str) -> tuple[datetime, datetime]:
    zi = ZoneInfo(tz)
    start = datetime.combine(d, datetime.min.time()).replace(tzinfo=zi)
    end = datetime.combine(d + timedelta(days=1), datetime.min.time()).replace(tzinfo=zi)
    return start, end


def invalidate() -> None:
    _cache.clear()


def _cached(key: str, fn, force: bool = False):
    hit = _cache.get(key)
    if hit and not force and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    val = fn()
    _cache[key] = (time.monotonic(), val)
    return val


def read_calendars(conn, uid: str) -> tuple[dict | None, list[dict]]:
    acct = A.get_account(conn, uid, "google")
    if A._override(uid, "google_calendar") is not None:
        acct = acct or {"id": "sim-google", "config": {}}
        return acct, rows(conn, "SELECT * FROM scope_grants WHERE user_id=? AND resource_type='calendar' AND access='read'",
                          (uid,)) or [{"resource_id": "primary", "resource_name": "Primary (simulated)"}]
    if not acct:
        return None, []
    return acct, A.grants(conn, acct["id"], "calendar", "read")


def events_between(conn, uid: str, start: datetime, end: datetime, force: bool = False) -> dict:
    """{"status", "detail", "events": [...normalized...], "last_sync"}"""
    try:
        cp = A.calendar_provider(conn, uid)
    except A.NotConnected as e:
        return {"status": "not_connected", "detail": str(e), "events": []}
    acct, cals = read_calendars(conn, uid)
    if not cals:
        return {"status": "not_connected", "detail": "No calendars selected for reading.", "events": []}

    def load():
        out, errors = [], []
        for g in cals:
            try:
                for ev in cp.list_events(g["resource_id"], iso(start), iso(end)):
                    if ev.status == "cancelled":
                        continue
                    out.append(_norm_event(ev, g.get("resource_name")))
            except ProviderError as e:
                errors.append(f"{g.get('resource_name') or g['resource_id']}: {e.kind}")
        out.sort(key=lambda e: e["sort"])
        return out, errors
    try:
        evs, errors = _cached(f"ev:{uid}:{iso(start)}:{iso(end)}", load, force)
    except ProviderError as e:
        return {"status": "unavailable", "detail": f"Calendar read failed: {e.kind}", "events": []}
    if acct and acct.get("id") and not A.is_simulated(cp):
        A.mark_synced(conn, acct["id"])
    status = "ok" if not errors else ("unavailable" if not evs else "partial")
    return {"status": status, "detail": "; ".join(errors) or None, "events": evs, "last_sync": now_iso(),
            "simulated": A.is_simulated(cp)}


def _norm_event(ev, cal_name: str | None) -> dict:
    if ev.all_day:
        start, end, sort = ev.start.get("date"), ev.end.get("date"), ev.start.get("date") + "T00"
    else:
        start, end = ev.start.get("dateTime"), ev.end.get("dateTime")
        sort = iso(parse_iso(start))
    return {"id": ev.id, "calendar": cal_name, "summary": ev.summary, "start": start, "end": end, "all_day": ev.all_day,
            "busy": ev.busy and not ev.all_day, "status": ev.status, "html_link": ev.html_link,
            "recurring": bool(ev.recurring_event_id), "sort": sort, "location": (ev.raw or {}).get("location")}


def tasks_snapshot(conn, uid: str, force: bool = False) -> dict:
    try:
        tp = A.task_provider(conn, uid)
    except A.NotConnected as e:
        pending = rows(conn, "SELECT content, due, status FROM task_links WHERE user_id=? AND status='pending_sync'", (uid,))
        return {"status": "not_connected", "detail": str(e), "tasks": [], "pending_sync": pending}
    try:
        tasks = _cached(f"tasks:{uid}", lambda: tp.list_tasks(), force)
    except ProviderError as e:
        return {"status": "unavailable", "detail": f"Todoist read failed: {e.kind}", "tasks": []}
    acct = A.get_account(conn, uid, "todoist")
    if acct and not A.is_simulated(tp):
        A.mark_synced(conn, acct["id"])
    out = [{"id": t.id, "content": t.content, "due": t.due, "priority": t.priority, "project_id": t.project_id, "url": t.url,
            "parent_id": t.parent_id, "labels": t.labels} for t in tasks if not t.is_completed and not t.is_deleted]
    return {"status": "ok", "tasks": out, "last_sync": now_iso(), "simulated": A.is_simulated(tp)}


def due_on_or_before(tasks: list[dict], d: date) -> tuple[list[dict], list[dict]]:
    due_today, overdue = [], []
    for t in tasks:
        dd = (t.get("due") or {}).get("date")
        if not dd:
            continue
        dd = date.fromisoformat(dd[:10])
        if dd == d:
            due_today.append(t)
        elif dd < d:
            overdue.append(t)
    return due_today, overdue


def deadlines_snapshot(conn, uid: str, start: date, days: int = 7) -> dict:
    acct = A.get_account(conn, uid, "ics")
    if not acct:
        return {"status": "not_connected", "detail": "No deadline feed configured.", "items": []}
    items = rows(conn, "SELECT id, summary, start_at, end_at, all_day, local_date, course, role, status, url, also_in_calendar "
                       "FROM deadline_items WHERE account_id=? AND status!='cancelled' AND local_date BETWEEN ? AND ? "
                       "ORDER BY local_date, start_at", (acct["id"], start.isoformat(), (start + timedelta(days=days)).isoformat()))
    last = acct.get("last_sync_at")
    status = "ok"
    if acct.get("last_error"):
        status = "unavailable" if not last else "stale"
    elif not last or parse_iso(last) < utcnow() - timedelta(hours=24):
        status = "stale"
    return {"status": status, "detail": acct.get("last_error") or ("Feed not refreshed in 24h" if status == "stale" else None),
            "items": [i | {"title": i["summary"], "due_at": i["start_at"] or i["local_date"]} for i in items], "last_sync": last,
            "note": "Deadline feed may be incomplete; it is not full Canvas access."}


def current_plan(conn, uid: str, d: date) -> dict | None:
    p = row(conn, "SELECT * FROM daily_plans WHERE user_id=? AND plan_date=?", (uid, d.isoformat()))
    if not p or not p["current_version"]:
        return None
    v = row(conn, "SELECT * FROM daily_plan_versions WHERE plan_id=? AND version=?", (p["id"], p["current_version"]))
    return {"plan_id": p["id"], "version_id": v["id"], "version": v["version"], "mission": v["mission"],
            "priorities": jload(v["priorities"], []), "constraints": jload(v["constraints"], []),
            "first_action": v["first_action"], "author": v["author"], "status": v["status"], "created_at": v["created_at"]}


GTA_EVENT_RE = re.compile(r"\b(gta|g\.t\.a\.|ta shift|office hours|is[ -]?640|msc[ -]?550|is[ -]?401|is[ -]?501|grading)\b", re.IGNORECASE)


def work_windows(events: list[dict], d: date, tz: str, day_start: int = 7, day_end: int = 22) -> list[dict]:
    zi = ZoneInfo(tz)
    cur = datetime.combine(d, datetime.min.time()).replace(hour=day_start, tzinfo=zi)
    end = datetime.combine(d, datetime.min.time()).replace(hour=day_end, tzinfo=zi)
    now_local = utcnow().astimezone(zi)
    if now_local.date() == d and now_local > cur:
        cur = now_local.replace(second=0, microsecond=0)
    busy = sorted((parse_iso(e["start"]), parse_iso(e["end"])) for e in events if e["busy"] and not e["all_day"])
    out = []
    for bs, be in busy:
        if be <= cur:
            continue
        if bs > cur and (bs - cur) >= timedelta(minutes=30):
            m = int((min(bs, end) - cur).total_seconds() // 60)
            out.append({"start": iso(cur), "end": iso(min(bs, end)), "minutes": m, "label": f"{m} min open"})
        cur = max(cur, be)
        if cur >= end:
            break
    if cur < end and (end - cur) >= timedelta(minutes=30):
        m = int((end - cur).total_seconds() // 60)
        out.append({"start": iso(cur), "end": iso(end), "minutes": m, "label": f"{m} min open"})
    return out


def conflicts(events: list[dict]) -> list[str]:
    timed = sorted([e for e in events if e["busy"] and not e["all_day"]], key=lambda e: e["sort"])
    out = []
    for a, b in zip(timed, timed[1:]):
        if parse_iso(b["start"]) < parse_iso(a["end"]):
            out.append(f"'{a['summary']}' overlaps '{b['summary']}'")
    return out


def _norm_task(text: str) -> str:
    t = re.sub(r"\s*\((overdue|deadline today[^)]*)\)\s*$", "", text or "", flags=re.I)
    return re.sub(r"\s+", " ", t).strip().lower()


def completed_today(conn, uid: str, d: date, tz: str, force: bool = False) -> dict[str, str]:
    """Tasks finished today -> when. JARVIS's own completions count immediately (task_links); completions made directly
    in Todoist come from its completed-tasks API. Positive evidence only: a plan line is never assumed done."""
    start, end = (iso(x) for x in day_bounds(d, tz))
    out: dict[str, str] = {}
    for r in rows(conn, "SELECT content, updated_at FROM task_links WHERE user_id=? AND status='completed' AND updated_at>=? "
                        "AND updated_at<?", (uid, start, end)):
        out[_norm_task(r["content"])] = r["updated_at"]
    try:
        tp = A.task_provider(conn, uid)
        for t in _cached(f"done:{uid}:{d}", lambda: tp.list_completed(start, end), force):
            out.setdefault(_norm_task(t.content), t.updated_at or end)
    except (A.NotConnected, ProviderError):
        pass
    return out


def plan_progress(conn, uid: str, plan: dict | None, d: date, tz: str, force: bool = False) -> dict | None:
    """The accepted plan stays the user's; progress is computed on read so the mission stays current as tasks get done."""
    if not plan:
        return None
    done = completed_today(conn, uid, d, tz, force)
    items = [{"text": p if isinstance(p, str) else str(p), "done": _norm_task(p if isinstance(p, str) else str(p)) in done}
             for p in plan.get("priorities") or []]
    all_done = bool(items) and all(i["done"] for i in items)
    nxt = next((i["text"] for i in items if not i["done"]), None)
    first = plan.get("first_action")
    # the written first action stands until the priority it points at is done; then the next open priority takes over
    first_done = bool(first) and any(i["done"] and _norm_task(first).startswith(_norm_task(i["text"])) for i in items)
    next_action = None if all_done else (first if first and not first_done else nxt)
    return plan | {"progress": items, "all_done": all_done, "next_action": next_action}


def today(conn, uid: str, force: bool = False, d: date | None = None) -> dict:
    tz = user_tz(conn, uid)
    d = d or local_today(conn, uid)
    start, end = day_bounds(d, tz)
    cal = events_between(conn, uid, start, end, force)
    tsk = tasks_snapshot(conn, uid, force)
    due_today, overdue = due_on_or_before(tsk["tasks"], d)
    dl = deadlines_snapshot(conn, uid, d)
    evs = cal["events"]
    return {
        "date": d.isoformat(), "tz": tz, "now": now_iso(),
        "plan": plan_progress(conn, uid, current_plan(conn, uid, d), d, tz, force),
        "agenda": evs, "tasks_due": due_today, "overdue": overdue, "deadlines": dl["items"],
        "conflicts": conflicts(evs),
        "work_windows": work_windows(evs, d, tz) if cal["status"] in ("ok", "partial") else [],
        "gta_events": [e for e in evs if GTA_EVENT_RE.search(e["summary"] or "")],
        "sources": {
            "calendar": {k: cal.get(k) for k in ("status", "detail", "last_sync", "simulated")},
            "todoist": {k: tsk.get(k) for k in ("status", "detail", "last_sync", "simulated")},
            "deadlines": {k: dl.get(k) for k in ("status", "detail", "last_sync", "note")},
        },
    }


def free_slots(conn, uid: str, d: date, minutes: int, part_of_day: str | None, tz: str) -> dict:
    start, end = day_bounds(d, tz)
    cal = events_between(conn, uid, start, end, force=True)
    if cal["status"] not in ("ok", "partial"):
        return {"status": cal["status"], "detail": cal.get("detail"), "slots": []}
    ranges = {"morning": (7, 12), "afternoon": (12, 17), "evening": (17, 22), None: (7, 22)}
    s_h, e_h = ranges.get(part_of_day, (7, 22))
    wins = work_windows(cal["events"], d, tz, s_h, e_h)
    slots = []
    for w in wins:
        ws, we = parse_iso(w["start"]), parse_iso(w["end"])
        # align to next quarter hour
        zi = ZoneInfo(tz)
        ls = ws.astimezone(zi)
        add = (15 - ls.minute % 15) % 15
        cur = ls + timedelta(minutes=add)
        while cur + timedelta(minutes=minutes) <= we.astimezone(zi) and len(slots) < 6:
            slots.append({"start": cur.isoformat(), "end": (cur + timedelta(minutes=minutes)).isoformat()})
            cur += timedelta(minutes=max(30, minutes))
    return {"status": "ok", "slots": slots, "events": cal["events"], "simulated": cal.get("simulated", False)}
