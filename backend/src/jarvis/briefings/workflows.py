"""Daily Command, Nightly Debrief, Weekly War Room — one code path for on-demand and scheduled runs.

Deterministic core (no model needed): refreshed sources, prioritization, windows, conflicts, first action.
Optional single bounded model call polishes the headline when budget allows. Cached by input hash.
Never invents weight, recovery, client compliance, income or meetings; unavailable sources are labeled.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.agents import context as ctx
from jarvis.agents.interpret import detect_area
from jarvis.briefings.notify import notify
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import canonical_hash, iso, new_id, now_iso, run_event, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError

KIND_LABEL = {"daily_command": "Daily Command", "nightly_debrief": "Nightly Debrief", "weekly_war_room": "Weekly War Room"}


def _fmt_t(s: str, tz: str) -> str:
    return datetime.fromisoformat(s).astimezone(ZoneInfo(tz)).strftime("%-I:%M %p")


def _task_rank(t: dict, today: date) -> tuple:
    due = (t.get("due") or {}).get("date")
    dd = date.fromisoformat(due[:10]) if due else date.max
    has_time = bool((t.get("due") or {}).get("datetime")) or (due and "T" in due)
    return (0 if dd < today else 1 if dd == today else 2, -(t.get("priority") or 1), 0 if has_time else 1, dd)


def _source_notes(sources: dict) -> list[str]:
    out = []
    for k, v in sources.items():
        if v["status"] != "ok":
            out.append(f"{k.title()}: {v['status'].replace('_', ' ')}" + (f" — {v['detail']}" if v.get("detail") else ""))
    return out


def daily_command(conn, uid: str, snap: dict) -> dict:
    tz, today = snap["tz"], date.fromisoformat(snap["date"])
    tasks = sorted(snap["tasks_due"] + snap["overdue"], key=lambda t: _task_rank(t, today))
    dls_today = [d for d in snap["deadlines"] if d["local_date"] == snap["date"]]
    candidates = [f"{d['summary']} (deadline today{' · ' + d['course'] if d.get('course') else ''})" for d in dls_today]
    candidates += [t["content"] + (" (overdue)" if (t.get("due") or {}).get("date", "9")[:10] < snap["date"] else "") for t in tasks]
    priorities = list(dict.fromkeys(candidates))[:3]
    plan = snap["plan"]
    source_notes = _source_notes(snap["sources"])
    if plan and plan["status"] == "accepted":
        mission, mission_src = plan["mission"], "accepted plan v%d" % plan["version"]
        priorities = plan["priorities"] or priorities
    elif plan:
        mission, mission_src = plan["mission"], "proposed plan v%d (not yet accepted)" % plan["version"]
    else:
        mission = f"Finish: {priorities[0]}" if priorities else None
        mission_src = "proposed by Daily Command — accept or edit it on Today"
    agenda = [f"{'All day' if e['all_day'] else _fmt_t(e['start'], tz)} {e['summary']}" for e in snap["agenda"]]
    windows = [f"{_fmt_t(w['start'], tz)}–{_fmt_t(w['end'], tz)} ({w['minutes']} min)" for w in snap["work_windows"]]
    obstacle = None
    if snap["conflicts"]:
        obstacle = "Conflict: " + snap["conflicts"][0]
    elif source_notes:
        obstacle = "Incomplete picture — " + source_notes[0]
    elif snap["work_windows"] and max(w["minutes"] for w in snap["work_windows"]) < 60:
        obstacle = "No open block longer than an hour today."
    elif len(tasks) > 6:
        obstacle = f"{len(tasks)} tasks due/overdue — more than fits; pick three."
    first_action = None
    if priorities:
        first_action = priorities[0]
        if snap["work_windows"]:
            first_action += f" — first open window {windows[0]}"
    gta_note = None
    if snap["gta_events"]:
        gta_note = "GTA shift(s) today: school work inside them is conditional on assigned GTA duties."
    return {"headline": f"{KIND_LABEL['daily_command']} — {today.strftime('%A %b %d')}",
            "mission": mission, "mission_source": mission_src, "priorities": priorities, "agenda": agenda,
            "due": [t["content"] for t in tasks], "deadlines": [f"{d['summary']} ({d['local_date']})" for d in snap["deadlines"]],
            "windows": windows, "conflicts": snap["conflicts"], "obstacle": obstacle, "first_action": first_action,
            "notes": ([gta_note] if gta_note else []) + source_notes,
            "sections": [{"title": "Agenda", "items": agenda or (["Calendar not available"] if snap["sources"]["calendar"]["status"]
                                                                  != "ok" else ["Nothing scheduled"])},
                         {"title": "Due / overdue", "items": [t["content"] for t in tasks] or (
                             ["Todoist not available"] if snap["sources"]["todoist"]["status"] != "ok" else ["Nothing due"])}]}


def nightly_debrief(conn, uid: str, snap: dict) -> dict:
    tz, today = snap["tz"], date.fromisoformat(snap["date"])
    start, end = ctx.day_bounds(today, tz)
    completed, comp_status = [], "not_connected"
    try:
        tp = A.task_provider(conn, uid)
        completed = [t.content for t in tp.list_completed(iso(start), iso(end))]
        comp_status = "ok"
    except A.NotConnected:
        pass
    except ProviderError as e:
        comp_status = f"unavailable ({e.kind})"
    verified = [r["expected_effect"] for r in rows(
        conn, "SELECT a.expected_effect FROM proposed_actions a JOIN verification_receipts r ON r.action_id=a.id "
              "JOIN proposal_versions v ON v.id=a.proposal_version_id JOIN proposals p ON p.id=v.proposal_id "
              "WHERE p.user_id=? AND r.ok=1 AND r.verified_at BETWEEN ? AND ?", (uid, iso(start), iso(end)))]
    open_today = [t["content"] for t in sorted(snap["tasks_due"] + snap["overdue"], key=lambda t: _task_rank(t, today))]
    tomorrow = (today + timedelta(days=1)).isoformat()
    tmr = [t["content"] for t in ctx.tasks_snapshot(conn, uid)["tasks"] if (t.get("due") or {}).get("date", "")[:10] == tomorrow]
    plan = snap["plan"]
    questions = ["What else did you finish today that isn't listed?", "Anything that changed or slipped?",
                 f"Carry these to tomorrow? {', '.join(open_today[:5])}" if open_today else "Anything to carry to tomorrow?"]
    first = tmr[0] if tmr else (open_today[0] if open_today else None)
    return {"headline": f"{KIND_LABEL['nightly_debrief']} — {today.strftime('%A %b %d')}",
            "mission": plan["mission"] if plan else None, "completed_observed": completed, "completed_status": comp_status,
            "jarvis_verified": verified, "still_open": open_today, "questions": questions,
            "first_action": f"Tomorrow first: {first}" if first else None, "priorities": [], "agenda": [], "due": open_today,
            "deadlines": [], "windows": [], "conflicts": [], "obstacle": None,
            "notes": _source_notes(snap["sources"]) + ([] if comp_status == "ok" else [f"Completed tasks: {comp_status}"]),
            "sections": [{"title": "Observed completions", "items": completed or [f"None observed ({comp_status})"]},
                         {"title": "Still open", "items": open_today or ["Nothing open that was due"]},
                         {"title": "Questions", "items": questions}]}


def weekly_war_room(conn, uid: str, snap: dict) -> dict:
    tz, today = snap["tz"], date.fromisoformat(snap["date"])
    tasks = ctx.tasks_snapshot(conn, uid)
    overdue = [t for t in tasks["tasks"] if (t.get("due") or {}).get("date", "9999")[:10] < snap["date"]]
    by_area: dict[str, int] = {}
    for t in overdue:
        a = detect_area(t["content"]) or "other"
        by_area[a] = by_area.get(a, 0) + 1
    start = ctx.day_bounds(today + timedelta(days=1), tz)[0]
    end = ctx.day_bounds(today + timedelta(days=7), tz)[1]
    cal = ctx.events_between(conn, uid, start, end, force=True)
    load: dict[str, float] = {}
    for e in cal["events"]:
        if e["busy"] and not e["all_day"]:
            d = datetime.fromisoformat(e["start"]).astimezone(ZoneInfo(tz)).strftime("%a")
            hrs = (datetime.fromisoformat(e["end"]) - datetime.fromisoformat(e["start"])).total_seconds() / 3600
            load[d] = round(load.get(d, 0) + hrs, 1)
    dls = ctx.deadlines_snapshot(conn, uid, today + timedelta(days=1), 7)
    upcoming_due = [t for t in tasks["tasks"] if snap["date"] < (t.get("due") or {}).get("date", "0")[:10] <=
                    (today + timedelta(days=7)).isoformat()]
    wk_start = iso(utcnow() - timedelta(days=7))
    done_cnt = row(conn, "SELECT COUNT(*) c FROM verification_receipts r JOIN proposed_actions a ON a.id=r.action_id "
                         "JOIN proposal_versions v ON v.id=a.proposal_version_id JOIN proposals p ON p.id=v.proposal_id "
                         "WHERE p.user_id=? AND r.ok=1 AND r.verified_at>?", (uid, wk_start))["c"]
    decisions = []
    if overdue:
        worst = max(by_area, key=by_area.get)
        decisions.append(f"Decide: reschedule, delegate or drop {by_area[worst]} overdue {worst} item(s) (proposals only on your say-so).")
    heavy = [d for d, h in load.items() if h >= 8]
    if heavy:
        decisions.append(f"Heavy days ({', '.join(heavy)}): protect one focus block or lighten commitments.")
    if dls["items"]:
        decisions.append(f"{len(dls['items'])} deadline(s) next week — confirm which own-course items need prep time.")
    pri = [d["summary"] for d in dls["items"][:2]] + [t["content"] for t in upcoming_due[:3]]
    return {"headline": f"{KIND_LABEL['weekly_war_room']} — week of {(today + timedelta(days=1)).strftime('%b %d')}",
            "mission": None, "priorities": list(dict.fromkeys(pri))[:3], "overdue_by_area": by_area,
            "calendar_load_hours": load, "jarvis_actions_verified_7d": done_cnt, "decisions": decisions,
            "agenda": [], "due": [t["content"] for t in upcoming_due], "deadlines": [f"{d['summary']} ({d['local_date']})"
                                                                                  for d in dls["items"]],
            "windows": [], "conflicts": [], "obstacle": decisions[0] if decisions else None,
            "first_action": "Pick next week's top three and tell me which to schedule." if pri else None,
            "notes": _source_notes(snap["sources"]) + ([f"Calendar: {cal['status']}"] if cal["status"] != "ok" else []),
            "sections": [{"title": "Overdue patterns", "items": [f"{k}: {v}" for k, v in by_area.items()] or ["None overdue"]},
                         {"title": "Calendar load next 7 days (busy hours)", "items": [f"{k}: {v}h" for k, v in load.items()]
                          or [f"Calendar {cal['status']}"]},
                         {"title": "Decisions", "items": decisions or ["No decisions forced this week"]}],
            "note": "No automatic rescheduling: changes happen only through approved proposals."}


BUILDERS = {"daily_command": daily_command, "nightly_debrief": nightly_debrief, "weekly_war_room": weekly_war_room}


def spoken_summary(kind: str, c: dict) -> str:
    parts = []
    if c.get("mission"):
        parts.append(f"Mission: {c['mission']}.")
    if kind == "daily_command":
        if c.get("priorities"):
            parts.append("Top priorities: " + "; ".join(c["priorities"][:3]) + ".")
        if c.get("obstacle"):
            parts.append(c["obstacle"] + ".")
        if c.get("first_action"):
            parts.append("First: " + c["first_action"].split(" — ")[0] + ".")
    elif kind == "nightly_debrief":
        parts.append(f"{len(c.get('completed_observed', []))} completions observed; {len(c.get('still_open', []))} still open.")
        parts.append(c["questions"][0])
    else:
        parts.append(" ".join(c.get("decisions", [])[:2]) or "No forced decisions this week.")
    missing = [n for n in c.get("notes", []) if "not connected" in n or "unavailable" in n]
    if missing:
        parts.append(f"Heads up: {len(missing)} source{'s' if len(missing) > 1 else ''} unavailable.")
    from jarvis.agents.chief import speechify
    return speechify(" ".join(parts))


def generate(conn: sqlite3.Connection, uid: str, kind: str, force: bool = False, run_id: str | None = None,
             schedule_run_id: str | None = None, catch_up: bool = False, scheduled: bool = False) -> dict:
    snap = ctx.today(conn, uid, force=True)
    content = BUILDERS[kind](conn, uid, snap)
    input_hash = canonical_hash({"kind": kind, "c": {k: content.get(k) for k in content if k != "headline"},
                                 "plan": (snap["plan"] or {}).get("version_id")})
    if not force:
        cached = row(conn, "SELECT * FROM briefings WHERE user_id=? AND kind=? AND for_date=? AND input_hash=? "
                           "ORDER BY created_at DESC LIMIT 1", (uid, kind, snap["date"], input_hash))
        if cached:
            if run_id:
                run_event(conn, run_id, "status", "Inputs unchanged — reusing cached briefing")
            return _view(cached)
    generated_by = "deterministic"
    from jarvis.llm.provider import LLMError, get_provider
    llm = get_provider()
    ok, _ = llm.available(conn)
    if ok and content.get("priorities"):
        try:
            from jarvis.agents.persona import persona
            out = llm.complete(conn, uid=uid, purpose=f"briefing:{kind}", scheduled=scheduled, run_id=run_id, role="briefing",
                               system=f"Write ONE punchy, direct sentence (max 25 words) that frames {persona(conn, uid).name}'s "
                                      "day from these facts. "
                                      "Do not add facts, tasks, meetings or numbers that are not listed. Do not change the mission.",
                               content=[{"type": "text", "text": jdump({k: content.get(k) for k in
                                                                        ("mission", "priorities", "obstacle", "first_action")})}],
                               max_tokens=120)
            content["headline_note"] = out["text"].strip()
            generated_by = f"deterministic+model:{out['model']}"
        except LLMError as e:
            content["notes"] = content.get("notes", []) + [f"Model polish skipped: {e.message}"]
    spoken = spoken_summary(kind, content)
    plan_vid = None
    with tx(conn):
        if kind == "daily_command" and not snap["plan"] and content.get("mission"):
            plan_vid = save_plan(conn, uid, date.fromisoformat(snap["date"]), content["mission"], content["priorities"], [],
                                 content.get("first_action"), author="briefing", status="proposed",
                                 reason="Proposed by Daily Command", in_tx=True)
        else:
            plan_vid = (snap["plan"] or {}).get("version_id")
        bid = new_id("brf")
        conn.execute("INSERT INTO briefings (id, user_id, kind, for_date, plan_version_id, input_hash, content, spoken_summary, "
                     "sources, generated_by, schedule_run_id, catch_up, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (bid, uid, kind, snap["date"], plan_vid, input_hash, jdump(content), spoken, jdump(snap["sources"]),
                      generated_by, schedule_run_id, int(catch_up), now_iso()))
        title = KIND_LABEL[kind] + (" (catch-up)" if catch_up else "")
        first = content.get("first_action") or content.get("obstacle") or "Open Jarvis for details."
        notify(conn, uid, "briefing", f"{title}: {first}"[:120], spoken, "briefing", bid)
    return _view(row(conn, "SELECT * FROM briefings WHERE id=?", (bid,)))


def _view(b: dict) -> dict:
    b = dict(b)
    b["content"] = jload(b["content"], {})
    b["sources"] = jload(b["sources"], {})
    b["catch_up"] = bool(b["catch_up"])
    return b


def render_text(b: dict) -> str:
    c = b["content"]
    lines = [c["headline"] + (" (catch-up)" if b.get("catch_up") else "")]
    if c.get("headline_note"):
        lines.append(c["headline_note"])
    if c.get("mission"):
        lines.append(f"Mission: {c['mission']}" + (f"  [{c['mission_source']}]" if c.get("mission_source") else ""))
    if c.get("priorities"):
        lines.append("Top 3: " + " | ".join(f"{i + 1}) {p}" for i, p in enumerate(c["priorities"][:3])))
    for s in c.get("sections", []):
        lines.append(f"{s['title']}: " + "; ".join(s["items"][:8]))
    if c.get("windows"):
        lines.append("Open windows: " + ", ".join(c["windows"][:4]))
    if c.get("obstacle"):
        lines.append(f"Likely obstacle: {c['obstacle']}")
    if c.get("first_action"):
        lines.append(f"First action: {c['first_action']}")
    for n in c.get("notes", []):
        lines.append(f"Note: {n}")
    return "\n".join(lines)


def save_plan(conn, uid: str, d: date, mission: str, priorities: list[str], constraints: list[str], first_action: str | None,
              author: str, status: str, reason: str | None, in_tx: bool = False) -> str:
    def body():
        p = row(conn, "SELECT * FROM daily_plans WHERE user_id=? AND plan_date=?", (uid, d.isoformat()))
        if not p:
            pid = new_id("plan")
            conn.execute("INSERT INTO daily_plans (id, user_id, plan_date, current_version, created_at) VALUES (?,?,?,0,?)",
                         (pid, uid, d.isoformat(), now_iso()))
            cur = 0
        else:
            pid, cur = p["id"], p["current_version"]
        v = cur + 1
        vid = new_id("dpv")
        conn.execute("UPDATE daily_plan_versions SET status='superseded' WHERE plan_id=? AND status!='superseded'", (pid,))
        conn.execute("INSERT INTO daily_plan_versions (id, plan_id, version, mission, priorities, constraints, first_action, author, "
                     "status, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (vid, pid, v, mission, jdump(priorities[:3]), jdump(constraints), first_action, author, status, reason,
                      now_iso()))
        conn.execute("UPDATE daily_plans SET current_version=? WHERE id=?", (v, pid))
        return vid
    if in_tx:
        return body()
    with tx(conn):
        return body()


def record_nightly_answers(conn, uid: str, briefing_id: str | None, answers: dict) -> dict:
    """User statements update local records directly; external completion still requires proposals."""
    if briefing_id and not row(conn, "SELECT id FROM briefings WHERE id=? AND user_id=?", (briefing_id, uid)):
        raise KeyError(briefing_id)
    did = new_id("dbf")
    conn.execute("INSERT INTO debriefs (id, user_id, kind, briefing_id, content, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (did, uid, "nightly", briefing_id, jdump(answers), now_iso(), now_iso()))
    return {"id": did, "content": answers,
            "note": "Recorded locally. To mark Todoist tasks complete, say 'mark X done' and approve the proposal."}
