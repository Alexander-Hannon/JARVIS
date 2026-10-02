"""Heads-up: the proactive layer on Today. Deterministic (no model calls), cheap, and truthful about missing sources.

Cards: pending approvals, calendar conflicts, deadlines in the next 48 h, leave-by for the next event with a location
(when travel time is connected), email needing a reply (cached 10 min), and recent mission findings.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from jarvis.agents import context as ctx
from jarvis.core.db import rows
from jarvis.core.util import iso, parse_iso, utcnow
from jarvis.integrations import accounts as A

_mail_cache: dict[str, tuple[float, dict]] = {}
_route_cache: dict[str, tuple[float, float]] = {}


def _mail(conn) -> dict | None:
    if not A.connected_mailboxes(conn):
        return None
    hit = _mail_cache.get("x")
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    from jarvis.mail import service as M
    r = M.inbox(conn, unread_only=True, limit=25)
    val = {"needs_reply": r["counts"].get("needs_reply", 0),
           "from": [i["from_name"] for i in r["items"] if i["category"] == "needs_reply"][:3]}
    _mail_cache["x"] = (time.monotonic(), val)
    return val


def _leave_by(conn, snap: dict) -> dict | None:
    if not A.app_connected(conn, "routing"):
        return None
    now = utcnow()
    nxt = next((e for e in snap["agenda"] if not e["all_day"] and e.get("location") and parse_iso(e["start"]) > now
                and parse_iso(e["start"]) - now < timedelta(hours=4)), None)
    if not nxt:
        return None
    key = nxt["location"]
    hit = _route_cache.get(key)
    if not hit or time.monotonic() - hit[0] > 1800:
        from jarvis.agents import weather as W
        home = W.location(conn)
        r = A.app_provider(conn, "routing")
        dest = r.geocode(key, (home["lat"], home["lon"]))
        if not dest:
            return None
        hit = (time.monotonic(), r.route((home["lat"], home["lon"]), (dest["lat"], dest["lon"]))["minutes"])
        _route_cache[key] = hit
    minutes = hit[1]
    leave = parse_iso(nxt["start"]) - timedelta(minutes=round(minutes * 1.15) + 5)   # no live traffic: buffer
    return {"event": nxt["summary"], "location": key, "drive_minutes": minutes, "leave_at": iso(leave)}


COURSE = re.compile(r"\b([A-Z]{2,4})[ -]?(\d{3})")


def split_deadline(summary: str) -> tuple[str, str | None]:
    """Canvas feed titles look like 'Title [(MKT 601-01/601-01J Merged) (FA26) LONG NAME]' -> ('Title', 'MKT 601')."""
    m = re.match(r"^(.*?)\s*\[(.*)\]\s*$", summary or "")
    title, bracket = (m.group(1), m.group(2)) if m else (summary or "", "")
    c = COURSE.search(bracket) or COURSE.search(summary or "")
    return title.strip() or (summary or "").strip(), f"{c.group(1)} {c.group(2)}" if c else None


def day_label(d: date, today: date) -> str:
    n = (d - today).days
    return "today" if n == 0 else "tomorrow" if n == 1 else d.strftime("%a")


def cards(conn) -> list[dict]:
    snap = ctx.today(conn)
    tz = ZoneInfo(snap["tz"])
    out: list[dict] = []
    pending = rows(conn, "SELECT id, title FROM proposals WHERE status='proposed' ORDER BY updated_at DESC LIMIT 5")
    if pending:
        out.append({"kind": "approvals", "tone": "attention", "title": f"{len(pending)} waiting for your approval",
                    "detail": "; ".join(p["title"] for p in pending[:3]), "link": "/actions"})
    try:
        lb = _leave_by(conn, snap)
    except Exception:  # noqa: BLE001 — a routing hiccup must not break Today
        lb = None
    if lb:
        at = parse_iso(lb["leave_at"]).astimezone(tz).strftime("%-I:%M %p")
        out.append({"kind": "leave_by", "tone": "attention", "title": f"Leave by {at} for {lb['event']}",
                    "detail": f"About {round(lb['drive_minutes'])} min to {lb['location']} (no live traffic; buffer added)",
                    "link": "/"})
    if snap["conflicts"]:
        n = len(snap["conflicts"])
        out.append({"kind": "conflict", "tone": "warn", "title": "Calendar conflict" if n == 1 else f"{n} calendar conflicts",
                    "detail": "", "items": [{"title": str(c).replace("'", "")} for c in snap["conflicts"][:3]], "link": "/"})
    if snap["deadlines"]:
        today = date.fromisoformat(snap["date"])
        dls = snap["deadlines"]
        items = []
        for d in dls[:4]:
            title, course = split_deadline(d["summary"])
            items.append({"title": title, "meta": course, "when": day_label(date.fromisoformat(d["local_date"]), today)})
        first = date.fromisoformat(dls[0]["local_date"])
        due_first = sum(1 for d in dls if d["local_date"] == dls[0]["local_date"])
        out.append({"kind": "deadlines", "tone": "info",
                    "title": f"{len(dls)} deadline{'s' if len(dls) != 1 else ''} this week",
                    "detail": f"{due_first} due {day_label(first, today)}" + (f", {len(dls) - 4} more later" if len(dls) > 4 else ""),
                    "items": items, "link": "/missions"})
    try:
        m = _mail(conn)
    except Exception:  # noqa: BLE001
        m = {"error": True}
    if m and m.get("needs_reply"):
        out.append({"kind": "email", "tone": "info", "title": f"{m['needs_reply']} email(s) may need a reply",
                    "detail": ", ".join(m["from"]), "link": "/inbox"})
    found = rows(conn, "SELECT title, body FROM notifications WHERE kind='mission' AND read_at IS NULL "
                       "ORDER BY created_at DESC LIMIT 2")
    for f in found:
        out.append({"kind": "mission", "tone": "info", "title": f["title"], "detail": f["body"][:220], "link": "/missions"})
    return out


def weekly_report(conn) -> dict:
    since = iso(utcnow() - timedelta(days=7))
    q = lambda sql: conn.execute(sql, (since,)).fetchone()[0]  # noqa: E731
    verified = q("SELECT COUNT(*) FROM proposed_actions WHERE state='verified' AND updated_at>=?")
    auto = q("SELECT COUNT(*) FROM approvals WHERE channel='policy' AND created_at>=?")
    missions = q("SELECT COUNT(*) FROM mission_runs WHERE started_at>=? AND status IN ('done','nothing_new')")
    commands = q("SELECT COUNT(*) FROM runs WHERE kind='command' AND started_at>=?")
    spend = q("SELECT COALESCE(SUM(u.cost_usd),0) FROM usage_records u WHERE u.created_at>=?")
    # A rough, stated estimate: ~2 min saved per completed action, ~3 per mission run, ~1 per answered command.
    minutes = verified * 2 + missions * 3 + commands
    return {"commands": commands, "actions_done": verified, "auto_approved": auto, "mission_runs": missions,
            "spend_usd": round(float(spend), 2), "minutes_saved_estimate": minutes,
            "estimate_basis": "~2 min per completed action, ~3 per mission run, ~1 per answered request"}
