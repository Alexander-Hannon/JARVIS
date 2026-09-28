"""Functional specialists. Each reads the canonical DB / providers through scoped helpers and returns
structured results; none keeps a private copy of truth, and none can replace the daily mission.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.actions.proposals import ActionSpec
from jarvis.agents import context as ctx
from jarvis.agents.interpret import Commitment
from jarvis.core import timeparse
from jarvis.core.db import jload, row, rows
from jarvis.core.util import iso, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError
from jarvis.memory import service as memory

AREA_LABEL = {"gta": "GTA", "school": "School", "business": "Business", "coaching": "Coaching", "faith": "Faith",
              "training": "Training", "ai_guru": "AI GURU", "admin": "Admin"}


# ------------------------------------------------------------------ Tasks specialist
def todoist_target(conn, area: str | None) -> tuple[str | None, str | None, str | None, list[str]]:
    """Returns (account_id, project_id, project_name, missing_fields). Never guesses a project from names."""
    if "todoist" in A._overrides:
        acct = A.get_account(conn, "todoist")
        aid = acct["id"] if acct else None
    else:
        acct = A.get_account(conn, "todoist")
        if not acct or acct["status"] == "revoked":
            return None, None, None, ["Todoist not connected (Settings → Integrations)"]
        aid = acct["id"]
    if not aid:
        return None, None, None, ["Todoist not connected (Settings → Integrations)"]
    data = jload(row(conn, "SELECT data FROM user_settings WHERE id=1")["data"], {})
    mapped = (data.get("todoist", {}).get("area_projects") or {}).get(area or "")
    if mapped and A.has_grant(conn, aid, "todoist_project", mapped, "write"):
        g = row(conn, "SELECT resource_name FROM scope_grants WHERE account_id=? AND resource_type='todoist_project' AND "
                      "resource_id=?", (aid, mapped))
        return aid, mapped, g["resource_name"] if g else None, []
    tgt = A.default_target(conn, aid, "todoist_project")
    if not tgt:
        return aid, None, None, ["Choose the Todoist project Jarvis may add tasks to (Settings → Integrations)"]
    return aid, tgt["resource_id"], tgt["resource_name"], []


def _task_args(c: Commitment, project_id: str | None, tz: str) -> tuple[dict, list[str]]:
    w = c.when
    args: dict = {"content": c.content, "project_id": project_id}
    notes = list(w.assumptions)
    if w.date and w.time:
        args["due_datetime"] = iso(timeparse.to_utc_instant(w.date, w.time, tz))
        args["due_local"] = f"{w.date} {w.time} {tz}"
        if w.duration_min:
            args["duration"], args["duration_unit"] = w.duration_min, "minute"
    elif w.date:
        args["due_date"] = w.date
    if w.part_of_day:
        args["description"] = f"Preferred time: {w.part_of_day}"
    if w.role == "due" and w.date:
        args["deadline_kind"] = "hard deadline (you said '" + (w.phrase or "") + "')"
    return args, notes


def find_duplicates(conn, content: str, due_date: str | None, exclude_proposal: str | None = None) -> list[dict]:
    hits = []
    snap = ctx.tasks_snapshot(conn)
    for t in snap["tasks"]:
        if memory.similarity(t["content"], content) >= 0.6:
            hits.append({"where": "Todoist", "content": t["content"], "due": (t.get("due") or {}).get("date"), "id": t["id"]})
    for tl in rows(conn, "SELECT content, due, status, external_id FROM task_links WHERE created_at > ? AND status IN "
                         "('active','pending_sync','unknown')", (iso(utcnow() - timedelta(days=14)),)):
        if memory.similarity(tl["content"], content) >= 0.6 and not any(h.get("id") == tl["external_id"] for h in hits):
            hits.append({"where": "created by Jarvis", "content": tl["content"], "due": (jload(tl["due"], {}) or {}).get("due_date"),
                         "id": tl["external_id"]})
    for pa in rows(conn, "SELECT a.args, p.id pid FROM proposed_actions a JOIN proposal_versions v ON v.id=a.proposal_version_id "
                         "JOIN proposals p ON p.id=v.proposal_id AND p.current_version=v.version "
                         "WHERE a.action_type='task.create' AND a.state IN ('proposed','queued','executing','unknown') "
                         "AND p.status IN ('proposed','queued','executing','unknown')"):
        if pa["pid"] == exclude_proposal:
            continue
        a = jload(pa["args"], {})
        if memory.similarity(a.get("content", ""), content) >= 0.6:
            hits.append({"where": "pending proposal", "content": a.get("content"), "due": a.get("due_date"), "id": None})
    return hits


def propose_tasks(conn, commitments: list[Commitment], tz: str) -> tuple[list[ActionSpec], list[str], list[str]]:
    specs, assumptions, questions = [], [], []
    for c in commitments:
        aid, pid, pname, missing = todoist_target(conn, c.area)
        args, notes = _task_args(c, pid, tz)
        assumptions += [f"“{c.content}”: {n}" for n in notes]
        dups = [] if c.duplicate_hint else find_duplicates(conn, c.content, args.get("due_date"))
        reason = f"From your request: “{c.text.strip()}”"
        if dups:
            d = dups[0]
            reason += f". Possible duplicate of {d['where']} task “{d['content']}”" + (f" (due {d['due']})" if d.get("due") else "")
            assumptions.append(f"“{c.content}” looks like an existing task — approve it only if you want a second one.")
        if c.when.vague and not c.when.date:
            assumptions.append(f"“{c.content}”: no date set ('{c.when.vague}' isn't a deadline).")
        eff = f"Create Todoist task “{c.content}”" + (f" due {args.get('due_local') or args.get('due_date')}" if c.when.date else "") \
            + (f" in {pname}" if pname else "")
        specs.append(ActionSpec(action_type="task.create", args=args, timezone=tz, expected_effect=eff, account_id=aid,
                                target_resource=pid, target_name=pname, after={"content": c.content, "due": args.get("due_date")
                                                                               or args.get("due_local")},
                                reason=reason, missing_fields=missing, preconditions={"duplicates_seen": len(dups)}))
    return specs, assumptions, questions


# ------------------------------------------------------------------ Calendar specialist
def calendar_target(conn) -> tuple[str | None, str | None, str | None, list[str], list[str]]:
    """(account_id, write_calendar_id, name, read_calendar_ids, missing)"""
    acct = A.get_account(conn, "google")
    if "google_calendar" in A._overrides:
        aid = acct["id"] if acct else None
    else:
        if not acct or "calendar" not in acct["config"].get("purposes", []):
            return None, None, None, [], ["Google Calendar not connected (Settings → Integrations)"]
        aid = acct["id"]
    if not aid:
        return None, None, None, [], ["Google Calendar not connected"]
    reads = [g["resource_id"] for g in A.grants(conn, aid, "calendar", "read")]
    tgt = A.default_target(conn, aid, "calendar")
    if not tgt:
        return aid, None, None, reads, ["Choose which calendar receives Jarvis blocks (Settings → Integrations)"]
    return aid, tgt["resource_id"], tgt["resource_name"], reads, []


def _event_summary(content: str) -> str:
    s = re.sub(r"^(block|schedule|book|find|carve out|reserve|put)( (some )?(time|a slot|a block|a session))?( on (my )?calendar)?"
               r"( for| to)?\s*", "", content, flags=re.IGNORECASE).strip()
    s = re.sub(r"\b(on|in) (my )?calendar\b", "", s, flags=re.IGNORECASE).strip(" ,.")
    return (s[0].upper() + s[1:]) if s else "Focus block"


def propose_event(conn, c: Commitment, tz: str) -> dict:
    """Returns {"specs", "assumptions", "questions", "message"} — never double-books, never moves fixed events."""
    aid, cal_id, cal_name, reads, missing = calendar_target(conn)
    minutes = c.when.duration_min
    questions, assumptions = [], list(c.when.assumptions)
    summary = _event_summary(c.content)
    if not minutes:
        questions.append(f"How long should “{summary}” be?")
    if not c.when.date:
        questions.append(f"Which day should I look for time for “{summary}”?")
    if questions:
        return {"specs": [], "assumptions": assumptions, "questions": questions[:1], "message": questions[0]}
    if missing and not cal_id:
        return {"specs": [], "assumptions": assumptions, "questions": [], "message": missing[0] + ". I can add it as a task instead."}
    d = date.fromisoformat(c.when.date)
    if c.when.time:
        start = timeparse.to_utc_instant(c.when.date, c.when.time, tz)
        slots_info = ctx.free_slots(conn, d, minutes, None, tz)
        if slots_info["status"] not in ("ok",):
            return {"specs": [], "assumptions": assumptions, "questions": [],
                    "message": f"I can't read your calendar right now ({slots_info['status']}), so I won't propose a block blind."}
        end = start + timedelta(minutes=minutes)
        clash = [e for e in slots_info["events"] if e["busy"] and not e["all_day"]
                 and datetime.fromisoformat(e["start"]) < end and datetime.fromisoformat(e["end"]) > start]
        if clash:
            alt = slots_info["slots"][:3]
            return {"specs": [], "assumptions": assumptions, "questions": [],
                    "message": f"{c.when.time} conflicts with “{clash[0]['summary']}”. I won't double-book or move fixed "
                               f"commitments. Open slots: " + ", ".join(_fmt_slot(s, tz) for s in alt) if alt else
                               "No open slot that day — something would have to give."}
        slot = {"start": start.astimezone(ZoneInfo(tz)).isoformat(), "end": end.astimezone(ZoneInfo(tz)).isoformat()}
        alts = []
    else:
        slots_info = ctx.free_slots(conn, d, minutes, c.when.part_of_day, tz)
        if slots_info["status"] != "ok":
            return {"specs": [], "assumptions": assumptions, "questions": [],
                    "message": f"I can't read your calendar ({slots_info['status']}: {slots_info.get('detail')}), so I won't "
                               f"guess a slot."}
        if not slots_info["slots"]:
            window = c.when.part_of_day or "that day"
            return {"specs": [], "assumptions": assumptions, "questions": [],
                    "message": f"No {minutes}-minute opening {window} on {d.strftime('%a %b %d')} without moving fixed "
                               f"commitments. Pick another day, a shorter block, or tell me what to drop."}
        slot, alts = slots_info["slots"][0], slots_info["slots"][1:4]
        assumptions.append(f"Picked the first open {minutes}-minute slot{' in the ' + c.when.part_of_day if c.when.part_of_day else ''}.")
    args = {"summary": summary, "start": {"dateTime": slot["start"], "timeZone": tz},
            "end": {"dateTime": slot["end"], "timeZone": tz}, "transparency": "opaque"}
    spec = ActionSpec(action_type="event.create", args=args, timezone=tz,
                      expected_effect=f"Add “{summary}” {_fmt_slot(slot, tz)} to {cal_name or cal_id}",
                      account_id=aid, target_resource=cal_id, target_name=cal_name,
                      after={"summary": summary, "start": slot["start"], "end": slot["end"]},
                      preconditions={"check_calendars": reads, "slot_free_at": iso(utcnow())},
                      reason=f"From your request: “{c.text.strip()}”", missing_fields=missing)
    msg = f"Open slot: {_fmt_slot(slot, tz)}."
    if alts:
        msg += " Alternatives: " + ", ".join(_fmt_slot(s, tz) for s in alts) + "."
    return {"specs": [spec], "assumptions": assumptions, "questions": [], "message": msg, "alternatives": alts}


def _fmt_slot(s: dict, tz: str) -> str:
    a = datetime.fromisoformat(s["start"]).astimezone(ZoneInfo(tz))
    b = datetime.fromisoformat(s["end"]).astimezone(ZoneInfo(tz))
    return f"{a.strftime('%a %b %d %-I:%M')}–{b.strftime('%-I:%M %p')}"


# ------------------------------------------------------------------ School/GTA specialist
def gta_queue(conn) -> dict:
    snap = ctx.today(conn)
    tz = snap["tz"]
    pref = [m for m in (memory.get(conn, r["id"], with_versions=False) for r in rows(
        conn, "SELECT id FROM memories WHERE entity='pref.schoolwork_during_gta' AND status='active' "
              "ORDER BY updated_at DESC LIMIT 1")) if m] or memory.list_(conn, q="schoolwork GTA shifts", status="active", limit=1)
    gta_tasks, school_tasks = [], []
    for t in snap["tasks_due"] + snap["overdue"]:
        area = _task_area(t["content"])
        (gta_tasks if area == "gta" else school_tasks if area == "school" else []).append(t)
    # upcoming school tasks beyond today (next 7 days) are queue candidates too
    for t in (ctx.tasks_snapshot(conn)["tasks"]):
        dd = (t.get("due") or {}).get("date")
        if dd and snap["date"] < dd[:10] <= (date.fromisoformat(snap["date"]) + timedelta(days=7)).isoformat():
            if _task_area(t["content"]) == "school" and t not in school_tasks:
                school_tasks.append(t)
    own_deadlines = [d for d in snap["deadlines"] if d["role"] == "own"]
    gta_deadlines = [d for d in snap["deadlines"] if d["role"] == "gta"]
    unknown_deadlines = [d for d in snap["deadlines"] if d["role"] not in ("own", "gta")]
    queue = sorted(school_tasks, key=lambda t: ((t.get("due") or {}).get("date") or "9999", -(t.get("priority") or 1)))
    lines = []
    if snap["plan"]:
        lines.append(f"Today's mission: {snap['plan']['mission']}")
    lines.append("GTA duties first:")
    if snap["gta_events"]:
        for e in snap["gta_events"]:
            lines.append(f"  • {e['summary']} ({_fmt_time(e, tz)})")
    for t in gta_tasks:
        lines.append(f"  • {t['content']}")
    for d in gta_deadlines:
        lines.append(f"  • {d['summary']} — {d['local_date']}")
    if not (snap["gta_events"] or gta_tasks or gta_deadlines):
        lines.append("  • None found in connected sources" + _source_caveat(snap))
    lines.append("If assigned GTA work leaves time, school queue (in order):")
    items = [f"{t['content']}" + (f" (due {(t.get('due') or {}).get('date')})" if t.get("due") else "") for t in queue]
    items += [f"{d['summary']} (due {d['local_date']})" for d in own_deadlines]
    lines += [f"  {i + 1}. {x}" for i, x in enumerate(items[:5])] or ["  • Nothing school-related found" + _source_caveat(snap)]
    if unknown_deadlines:
        lines.append(f"{len(unknown_deadlines)} deadline(s) aren't mapped to own/GTA yet — confirm course mappings in Settings.")
    basis = pref[0]["content"] if pref else None
    return {"text": "\n".join(lines), "spoken": _spoken_gta(gta_tasks, snap, items), "basis": basis,
            "sources": snap["sources"]}


def _source_caveat(snap: dict) -> str:
    down = [k for k, v in snap["sources"].items() if v["status"] not in ("ok",)]
    return f" (not connected/unavailable: {', '.join(down)})" if down else ""


def _spoken_gta(gta_tasks, snap, items) -> str:
    n_gta = len(gta_tasks) + len(snap["gta_events"])
    first = items[0] if items else None
    s = f"{n_gta} GTA item{'s' if n_gta != 1 else ''} first."
    if first:
        s += f" If time allows, start with {first}."
    return s


def _task_area(content: str) -> str | None:
    from jarvis.agents.interpret import detect_area
    return detect_area(content)


def _fmt_time(e: dict, tz: str) -> str:
    if e["all_day"]:
        return "all day"
    a = datetime.fromisoformat(e["start"]).astimezone(ZoneInfo(tz))
    b = datetime.fromisoformat(e["end"]).astimezone(ZoneInfo(tz))
    return f"{a.strftime('%-I:%M')}–{b.strftime('%-I:%M %p')}"


# ------------------------------------------------------------------ Complete-task helper (Tasks specialist)
def propose_completion(conn, text: str, tz: str) -> dict:
    try:
        tp = A.task_provider(conn)
    except A.NotConnected as e:
        return {"specs": [], "message": str(e)}
    target = re.sub(r"^(mark|check off|complete|close)\s+|(\s+as)?\s+(done|complete|completed)\.?$|^(i )?(finished|completed|am done with)\s+",
                    "", text.strip(), flags=re.IGNORECASE).strip(" .")
    try:
        tasks = tp.list_tasks()
    except ProviderError as e:
        return {"specs": [], "message": f"Todoist read failed ({e.kind}); nothing proposed."}
    scored = sorted(((memory.similarity(t.content, target), t) for t in tasks if not t.is_completed), key=lambda x: -x[0])
    cands = [t for s, t in scored if s >= 0.34][:3]
    if not cands:
        return {"specs": [], "message": f"I couldn't find an open Todoist task matching “{target}”."}
    if len(cands) > 1 and memory.similarity(cands[0].content, target) - memory.similarity(cands[1].content, target) < 0.15:
        return {"specs": [], "message": "Which one? " + "; ".join(f"{i + 1}) {t.content}" for i, t in enumerate(cands)),
                "questions": ["Which task did you finish?"]}
    t = cands[0]
    acct = A.get_account(conn, "todoist")
    sub = [x for x in tasks if x.parent_id == t.id and not x.is_completed]
    note = f" It has {len(sub)} open subtask(s); Jarvis will not complete them unless you list them." if sub else ""
    spec = ActionSpec(action_type="task.complete", args={"task_id": t.id, "content": t.content}, timezone=tz,
                      expected_effect=f"Mark “{t.content}” complete in Todoist.{note}", account_id=acct["id"] if acct else None,
                      target_resource=t.project_id, before={"content": t.content, "completed": False},
                      after={"completed": True}, reason=f"You said: “{text}”")
    return {"specs": [spec], "message": f"Mark “{t.content}” complete?{note}"}


# ------------------------------------------------------------------ Training / Business summaries (no autonomous plan changes)
def area_summary(conn, area: str) -> str:
    proj = row(conn, "SELECT * FROM projects WHERE area=?", (area,))
    mems = memory.list_(conn, status="active", project_id=proj["id"] if proj else None, limit=6) if proj else []
    lines = [f"{AREA_LABEL.get(area, area)} — current context (from your records):"]
    lines += [f"  • {m['content']} (source: {m['source_kind']}, {m['observed_at'][:10]})" for m in mems] or ["  • No records yet."]
    if area in ("business", "coaching"):
        lines.append("Income, balances, client check-ins and bills only come from sources you supply; none are invented here.")
    return "\n".join(lines)
