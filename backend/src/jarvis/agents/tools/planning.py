"""Read tools over AJ's day: calendar, tasks, deadlines, weather, briefings, recent actions, GitHub."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.agents import context as ctx
from jarvis.agents import specialists as S
from jarvis.agents.tools import obj, tool
from jarvis.core.db import rows
from jarvis.core.util import run_event
from jarvis.memory import service as memory

AREAS = ["school", "gta", "business", "coaching", "faith", "training", "ai_guru", "admin"]


def _day(s: str) -> date:
    return date.fromisoformat(s[:10])


@tool("get_today_overview",
      "Today's snapshot: current daily mission/priorities, agenda, due & overdue Todoist tasks, deadlines, "
      "open work windows, conflicts, and which data sources are connected. Use for planning questions.",
      obj({}), risk="read", family="chief", example="What does my day look like?")
def get_today_overview(run):
    from jarvis.agents import visuals as V
    run_event(run.conn, run.run_id, "tool", "Reading today's calendar, tasks and deadlines")
    snap = ctx.today(run.conn)
    d0 = date.fromisoformat(snap["date"])
    run.visuals.setdefault(f"cal:{d0}:1", V.calendar(run.conn, d0, 1, run.tz))
    if snap["tasks_due"] or snap["overdue"]:
        run.visuals["tasks:today"] = V.tasks(snap["overdue"] + snap["tasks_due"], "Due today & overdue")
    return {"date": snap["date"], "plan": snap["plan"],
            "agenda": [{k: e[k] for k in ("summary", "start", "end", "all_day")} for e in snap["agenda"]],
            "due_today": [t["content"] for t in snap["tasks_due"]], "overdue": [t["content"] for t in snap["overdue"]],
            "deadlines": [{k: d[k] for k in ("summary", "local_date", "role")} for d in snap["deadlines"]],
            "work_windows": snap["work_windows"], "conflicts": snap["conflicts"], "sources": snap["sources"]}


@tool("get_calendar", "Read events from AJ's selected Google calendars for a date range (read-only).",
      obj({"start_date": {"type": "string", "description": "YYYY-MM-DD (local)"},
           "days": {"type": "integer", "minimum": 1, "maximum": 14}}, ["start_date"]),
      risk="read", family="calendar", example="What's on my calendar Thursday?")
def get_calendar(run, start_date: str, days: int = 1):
    from jarvis.agents import visuals as V
    run_event(run.conn, run.run_id, "tool", "Reading your calendar")
    d0 = _day(start_date)
    n = max(1, min(days, 14))
    start = ctx.day_bounds(d0, run.tz)[0]
    end = ctx.day_bounds(d0 + timedelta(days=n - 1), run.tz)[1]
    cal = ctx.events_between(run.conn, start, end, force=True)
    zi = ZoneInfo(run.tz)
    run.visuals[f"cal:{d0}:{n}"] = V.calendar(run.conn, d0, min(n, 7), run.tz, cal)

    def loc(s, all_day):
        return s if all_day else datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(zi).strftime("%a %b %-d %-I:%M %p")
    return {"status": cal["status"], "detail": cal.get("detail"),
            "events": [{"summary": e["summary"], "start": loc(e["start"], e["all_day"]), "end": loc(e["end"], e["all_day"]),
                        "all_day": e["all_day"], "calendar": e.get("calendar")} for e in cal["events"]]}


@tool("find_free_time", "Find open slots of a given length on a date (respects existing events).",
      obj({"date": {"type": "string"}, "minutes": {"type": "integer", "minimum": 5, "maximum": 480},
           "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening"]}}, ["date", "minutes"]),
      risk="read", family="calendar", example="When am I free for two hours tomorrow afternoon?")
def find_free_time(run, date: str, minutes: int, part_of_day: str | None = None):
    run_event(run.conn, run.run_id, "tool", "Checking your free time")
    r = ctx.free_slots(run.conn, _day(date), minutes, part_of_day, run.tz)
    if r.get("slots"):
        from jarvis.agents import visuals as V
        run.visuals[f"slots:{date}"] = V.slots(_day(date), minutes, r["slots"])
    return {"status": r["status"], "slots": r.get("slots", [])[:6], "detail": r.get("detail")}


@tool("list_tasks", "List AJ's open Todoist tasks (read-only), optionally filtered by a text query or due date.",
      obj({"query": {"type": "string"}, "due_on_or_before": {"type": "string", "description": "YYYY-MM-DD"}}),
      risk="read", family="tasks", example="What's left on my to-do list this week?")
def list_tasks(run, query: str | None = None, due_on_or_before: str | None = None):
    run_event(run.conn, run.run_id, "tool", "Reading your Todoist tasks")
    snap = ctx.tasks_snapshot(run.conn)
    tasks = snap["tasks"]
    if query:
        tasks = [t for t in tasks if memory.similarity(t["content"], query) > 0.2 or query.lower() in t["content"].lower()]
    if due_on_or_before:
        tasks = [t for t in tasks if (t.get("due") or {}).get("date") and t["due"]["date"][:10] <= due_on_or_before]
    if tasks:
        from jarvis.agents import visuals as V
        run.visuals["tasks:list"] = V.tasks(sorted(tasks, key=lambda t: (t.get("due") or {}).get("date") or "9999"),
                                            f"Tasks{' matching ' + repr(query) if query else ''}")
    return {"status": snap["status"], "detail": snap.get("detail"),
            "tasks": [{"content": t["content"], "due": (t.get("due") or {}).get("date"), "priority": t.get("priority")}
                      for t in tasks[:40]]}


@tool("get_deadlines", "Course deadlines from AJ's deadline feed for the next N days (own coursework vs GTA).",
      obj({"days": {"type": "integer", "minimum": 1, "maximum": 30}}),
      risk="read", family="school_gta", example="Any deadlines coming up in the next two weeks?")
def get_deadlines(run, days: int = 7):
    run_event(run.conn, run.run_id, "tool", "Reading course deadlines")
    dl = ctx.deadlines_snapshot(run.conn, ctx.local_today(run.conn), days)
    return {"status": dl["status"], "note": dl.get("note"),
            "items": [{k: d[k] for k in ("summary", "local_date", "course", "role")} for d in dl["items"]]}


@tool("get_weather", "Daily forecast for AJ's location (default Huntsville, AL) for a date within the next 7 days.",
      obj({"date": {"type": "string", "description": "YYYY-MM-DD"}}, ["date"]),
      risk="read", family="weather", example="Do I need an umbrella tomorrow?")
def get_weather(run, date: str):
    from jarvis.agents import visuals as V
    from jarvis.agents import weather as W
    loc = W.location(run.conn)
    run_event(run.conn, run.run_id, "tool", f"Checking the forecast for {loc['name']}")
    try:
        data = W.fetch(loc, run.tz)
    except W.WeatherUnavailable as e:
        return {"error": str(e)}
    f = W.forecast_for(data, _day(date))
    run.visuals["weather"] = V.weather(data, loc, _day(date))
    return f | {"location": loc["name"], "units": "°F, mph"} if f else {"error": "outside the 7-day forecast"}


@tool("gta_shift_plan", "GTA obligations first, then a conditional queue of AJ's own schoolwork for a GTA shift.",
      obj({}), risk="read", family="school_gta", example="What should I work on during my GTA shift?")
def gta_shift_plan(run):
    run_event(run.conn, run.run_id, "tool", "Building your GTA-shift queue")
    r = S.gta_queue(run.conn)
    return {"plan": r["text"], "preference": r.get("basis")}


@tool("project_summary", "Current records for one life area.",
      obj({"area": {"type": "string", "enum": AREAS}}, ["area"]),
      risk="read", family="chief", example="Where do things stand with the business?")
def project_summary(run, area: str):
    return {"summary": S.area_summary(run.conn, area)}


@tool("generate_briefing", "Generate the Daily Command, Nightly Debrief or Weekly War Room briefing.",
      obj({"kind": {"type": "string", "enum": ["daily_command", "nightly_debrief", "weekly_war_room"]}}, ["kind"]),
      risk="read", family="briefing", example="Give me my daily command.")
def generate_briefing(run, kind: str):
    from jarvis.briefings import workflows
    run_event(run.conn, run.run_id, "tool", f"Generating {kind.replace('_', ' ')}")
    b = workflows.generate(run.conn, kind, run_id=run.run_id)
    return {"briefing": workflows.render_text(b)}


@tool("recent_actions", "What Jarvis recently proposed/executed (tasks/events) with verification status.",
      obj({}), risk="read", family="chief", example="Did that calendar block actually get added?")
def recent_actions(run):
    acts = rows(run.conn, "SELECT a.expected_effect, a.state, a.external_url, a.updated_at FROM proposed_actions a "
                          "WHERE a.state NOT IN ('superseded','skipped') ORDER BY a.updated_at DESC LIMIT 12")
    return {"actions": acts}


@tool("github_status", "Open issues/PRs for AJ's selected GitHub repos (read-only).",
      obj({}), risk="read", family="github", example="Any open PRs on my repos?")
def github_status(run):
    from jarvis.integrations.service import github_status as gs
    return {"repos": gs(run.conn) or "GitHub not connected"}
