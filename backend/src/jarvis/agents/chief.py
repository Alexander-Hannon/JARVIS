"""Chief/router: resolve intent, retrieve only relevant context, route to one (or a few read-only)
specialists, produce one coherent response + at most one active proposal per conversation.

Run limits (application-enforced): max specialists, max model calls, wall time. Only concise events
are persisted (no chain-of-thought).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime

from jarvis.actions import proposals as P
from jarvis.agents import context as ctx
from jarvis.agents import specialists as S
from jarvis.agents import agent
from jarvis.llm.provider import LLMError
from jarvis.agents.interpret import Commitment, Interpretation, classify, segment_utterance
from jarvis.config import get_settings
from jarvis.core import timeparse
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, new_id, now_iso, parse_iso, record_latency, run_event
from jarvis.memory import service as memory

log = logging.getLogger("jarvis.chief")
RUN_LIMITS = {"max_specialists": 3, "max_model_calls": 3, "max_seconds": 90}


@dataclass
class CommandInput:
    user_id: str                 # who is speaking: every read, proposal and memory is scoped to this user
    conversation_id: str
    text: str
    channel: str                 # voice | typed
    captured_at: str
    tz: str
    capture_id: str | None = None
    transcript_version: int | None = None
    speech_playing: bool = False
    attachments: list[str] | None = None
    session_id: str | None = None
    client_run_id: str | None = None
    origin: str = "command"       # command | channel | mission: decides which auto-approve rules may apply


class Reply:
    def __init__(self, defer: bool = False):
        # defer=True (multi-request utterances): proposal specs are collected and merged into ONE plan at the end.
        self.defer = defer
        self.deferred: list[tuple] = []
        self.text_lines: list[str] = []
        self.spoken: str | None = None
        self.proposal_id: str | None = None
        self.questions: list[str] = []
        self.citations: list[dict] = []
        self.visuals: list[dict] = []
        self.status = "completed"

    def say(self, s: str):
        self.text_lines.append(s)


def ensure_conversation(conn, uid: str, conversation_id: str | None) -> str:
    if conversation_id and row(conn, "SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, uid)):
        return conversation_id
    # A client-chosen id is honored only if it's well-formed and unused; someone else's conversation is never reused.
    fresh = conversation_id and re.fullmatch(r"conv_[a-f0-9]{20}", conversation_id) and \
        not row(conn, "SELECT 1 FROM conversations WHERE id=?", (conversation_id,))
    cid = conversation_id if fresh else new_id("conv")
    conn.execute("INSERT INTO conversations (id, user_id, created_at, updated_at) VALUES (?,?,?,?)",
                 (cid, uid, now_iso(), now_iso()))
    return cid


def _typed_capture(conn, inp: CommandInput) -> str:
    cid = new_id("cap")
    conn.execute("INSERT INTO captures (id, user_id, conversation_id, kind, captured_at, capture_tz, status, created_at) "
                 "VALUES (?,?,?,?,?,?, 'transcribed', ?)",
                 (cid, inp.user_id, inp.conversation_id, "text", inp.captured_at, inp.tz, now_iso()))
    conn.execute("INSERT INTO transcript_versions (id, capture_id, version, text, source, created_at) VALUES (?,?,1,?, 'typed', ?)",
                 (new_id("tv"), cid, inp.text, now_iso()))
    return cid


def handle(conn: sqlite3.Connection, inp: CommandInput) -> dict:
    t0 = time.perf_counter()
    uid = inp.user_id
    inp.conversation_id = ensure_conversation(conn, uid, inp.conversation_id)
    tv = None
    if inp.capture_id:
        cap = row(conn, "SELECT * FROM captures WHERE id=? AND user_id=?", (inp.capture_id, uid))
        if not cap:
            raise ValueError("Unknown capture.")
        tv = row(conn, "SELECT * FROM transcript_versions WHERE capture_id=? AND version=?",
                 (inp.capture_id, inp.transcript_version)) if inp.transcript_version else \
            row(conn, "SELECT * FROM transcript_versions WHERE capture_id=? ORDER BY version DESC LIMIT 1", (inp.capture_id,))
        if not tv:
            raise ValueError("Capture has no transcript yet.")
        inp.text = tv["text"]
        # Keep the ORIGINAL reference time for relative dates, even if processed later.
        inp.captured_at, inp.tz = cap["captured_at"], cap["capture_tz"]
        prior = row(conn, "SELECT * FROM runs WHERE capture_id=? AND json_extract(intent,'$.tv')=? AND status!='failed' "
                          "ORDER BY started_at DESC LIMIT 1", (inp.capture_id, tv["version"]))
        if prior and jload(prior["intent"], {}).get("response"):
            resp = jload(prior["intent"])["response"]
            resp["replayed"] = True
            return resp   # idempotent: same transcript version is never processed twice
    else:
        inp.capture_id = _typed_capture(conn, inp)

    run_id = inp.client_run_id if inp.client_run_id and not row(conn, "SELECT 1 FROM runs WHERE id=?", (inp.client_run_id,)) \
        else new_id("run")
    conn.execute("INSERT INTO runs (id, user_id, conversation_id, capture_id, kind, status, limits, started_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (run_id, uid, inp.conversation_id, inp.capture_id, "command", "running", jdump(RUN_LIMITS), now_iso()))
    conn.execute("INSERT INTO messages (id, conversation_id, role, text, capture_id, run_id, created_at) VALUES (?,?,?,?,?,?,?)",
                 (new_id("msg"), inp.conversation_id, "user", inp.text, inp.capture_id, run_id, now_iso()))
    active = P.active_for_conversation(conn, uid, inp.conversation_id)
    captured = parse_iso(inp.captured_at)
    interp = classify(inp.text, has_active_proposal=bool(active), has_attachments=bool(inp.attachments),
                      captured_at=captured, tz=inp.tz)
    reply = Reply()
    specialists: list[str] = []
    use_agent = interp.intent not in DETERMINISTIC_ONLY and agent.available(conn)[0]
    parts = [] if use_agent else _plan_requests(inp, interp, bool(active), captured)
    try:
        if use_agent:
            try:
                agent.run(conn, inp, reply, run_id, specialists,
                          agent.Scope(origin=inp.origin) if inp.origin != "command" else agent.COMMAND_SCOPE)
                interp = Interpretation("agent")
            except LLMError as e:
                run_event(conn, run_id, "error", f"Reasoning model unavailable ({e.kind}); offline mode")
                reply = Reply()
                reply.say(f"(My reasoning model is unavailable — {e.message} Using offline mode.)")
                parts = _plan_requests(inp, interp, bool(active), captured)
                use_agent = False
        if use_agent:
            pass
        elif len(parts) <= 1:
            run_event(conn, run_id, "route", f"Understood as: {interp.intent.replace('_', ' ')}")
            _dispatch(conn, inp, parts[0] if parts else interp, reply, run_id, active, captured, tv, specialists)
        else:
            interp = Interpretation("multi")
            _dispatch_multi(conn, inp, parts, reply, run_id, active, captured, tv, specialists)
    except Exception as e:  # visible, recoverable failure — never fabricated success
        log.exception("command failed")
        reply.say(f"Something failed while handling that ({type(e).__name__}). Nothing was changed.")
        reply.status = "failed"
        run_event(conn, run_id, "error", f"Run failed: {type(e).__name__}")
    if reply.proposal_id:
        P.mark_presented(conn, uid, reply.proposal_id)
    text = "\n".join(reply.text_lines).strip()
    spoken = speechify(reply.spoken or _spoken(text))
    mid = new_id("msg")
    conn.execute("INSERT INTO messages (id, conversation_id, role, text, spoken_text, capture_id, run_id, proposal_id, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (mid, inp.conversation_id, "assistant", text, spoken, inp.capture_id, run_id,
                                              reply.proposal_id, now_iso()))
    status = reply.status if reply.status != "completed" or not reply.questions else "needs_clarification"
    events = rows(conn, "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
    for e in events:
        e["data"] = jload(e["data"], {})
    resp = {"run_id": run_id, "conversation_id": inp.conversation_id, "status": status, "intent": interp.intent,
            "specialists": specialists,
            "message": {"id": mid, "role": "assistant", "text": text, "spoken_text": spoken, "proposal_id": reply.proposal_id,
                        "capture_id": inp.capture_id, "created_at": now_iso()},
            "proposal": P.view(conn, uid, reply.proposal_id) if reply.proposal_id else None, "questions": reply.questions,
            "citations": reply.citations, "visuals": reply.visuals, "events": events}
    conn.execute("UPDATE runs SET status=?, intent=?, specialists=?, finished_at=? WHERE id=?",
                 (status, jdump({"intent": interp.intent, "tv": tv["version"] if tv else 1, "response": resp}),
                  jdump(specialists), now_iso(), run_id))
    conn.execute("UPDATE conversations SET updated_at=?, title=COALESCE(title, ?) WHERE id=?",
                 (now_iso(), inp.text[:60], inp.conversation_id))
    record_latency(conn, "first_response", (time.perf_counter() - t0) * 1000, {"intent": interp.intent})
    if reply.proposal_id:
        record_latency(conn, "proposal", (time.perf_counter() - t0) * 1000)
    return resp


# These never go through the model: approvals, consent and plan edits are decided by deterministic code only.
DETERMINISTIC_ONLY = {"approve", "vague_consent", "cancel", "correct", "remove_item"}

SOLO_INTENTS = {"approve", "vague_consent", "cancel", "correct", "remove_item", "retrieve_actions", "remember",
                "forget_memory", "meeting_debrief", "local_file", "knowledge", "summarize"}


def _plan_requests(inp: CommandInput, whole: Interpretation, has_active: bool, captured: datetime) -> list[Interpretation]:
    """One utterance may hold several different requests ("add a task…, give me my calendar, and the weather").
    Split into requests and classify each; merge task captures into one. Approvals/corrections/etc. stay whole."""
    if whole.intent in SOLO_INTENTS or inp.attachments:
        return [whole]
    segs = segment_utterance(inp.text)
    if len(segs) <= 1:
        return [whole]
    parts = [classify(sg, has_active_proposal=has_active, has_attachments=False, captured_at=captured, tz=inp.tz)
             for sg in segs]
    if any(p.intent in SOLO_INTENTS for p in parts):
        return [whole]
    # low-confidence "question" fragments inside a task list are almost always task text; keep whole-utterance behavior
    kinds = {p.intent for p in parts}
    if kinds <= {"capture", "question"} and any(p.intent == "question" and p.confidence == "low" for p in parts):
        return [whole]
    merged: list[Interpretation] = []
    cap: Interpretation | None = None
    for p in parts:
        if p.intent == "capture":
            if cap is None:
                cap = Interpretation("capture", commitments=list(p.commitments))
                merged.append(cap)
            else:
                cap.commitments += p.commitments
        else:
            merged.append(p)
    return merged


SECTION = {"capture": "Tasks", "schedule": "Calendar block", "agenda": "Calendar", "weather": "Weather", "brief": "Daily Command",
           "debrief": "Nightly Debrief", "war_room": "War Room", "gta_queue": "GTA shift", "complete": "Complete task",
           "area_summary": "Summary", "question": "Answer"}


def _dispatch_multi(conn, inp: CommandInput, parts: list[Interpretation], reply: Reply, run_id: str, active, captured,
                    tv, specialists: list[str]) -> None:
    run_event(conn, run_id, "route", f"Understood {len(parts)} requests: " + ", ".join(p.intent.replace("_", " ") for p in parts))
    spoken: list[str] = []
    buf = Reply(defer=True)
    for p in parts[: RUN_LIMITS["max_specialists"] + 2]:
        sub = Reply(defer=True)
        try:
            _dispatch(conn, inp, p, sub, run_id, active, captured, tv, specialists)
        except Exception as e:  # one failing request must not sink the others
            log.exception("sub-request failed")
            sub.say(f"That part failed ({type(e).__name__}); nothing was changed.")
        reply.say(f"▸ {SECTION.get(p.intent, p.intent.replace('_', ' ').title())}")
        reply.text_lines += sub.text_lines
        reply.questions += sub.questions
        reply.citations += sub.citations
        reply.visuals += sub.visuals
        buf.deferred += sub.deferred
        if sub.spoken or sub.text_lines:
            spoken.append(sub.spoken or _spoken("\n".join(sub.text_lines)))
    if buf.deferred:
        specs, summaries, assumptions, questions = [], [], [], []
        for _title, sp, summary, asm, qs in buf.deferred:
            specs += sp
            summaries.append(summary)
            assumptions += asm
            questions += qs
        n = len(specs)
        _new_proposal(conn, inp, reply, run_id, f"{n} item{'s' if n != 1 else ''} from your request", specs,
                      "; ".join(summaries), assumptions, questions)
    reply.spoken = " ".join(x.strip() for x in spoken if x)


def _agenda(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str) -> None:
    from zoneinfo import ZoneInfo
    tz = inp.tz
    uid = inp.user_id
    day = date.fromisoformat(it.new_when.date) if it.new_when and it.new_when.date else ctx.local_today(conn, uid)
    label = _spoken_date(day.isoformat(), tz)
    run_event(conn, run_id, "tool", "Reading selected calendars")
    start, end = ctx.day_bounds(day, tz)
    cal = ctx.events_between(conn, uid, start, end, force=True)
    if cal["status"] not in ("ok", "partial"):
        reply.say(f"I can't read your calendar ({cal['status'].replace('_', ' ')}): {cal.get('detail') or ''}".strip())
        reply.spoken = "I can't read your calendar right now."
        return
    evs = cal["events"]
    zi = ZoneInfo(tz)
    from jarvis.agents import visuals as V
    reply.visuals.append(V.calendar(conn, uid, day, 1, tz, cal))

    def t(e):
        return "All day" if e["all_day"] else datetime.fromisoformat(e["start"]).astimezone(zi).strftime("%-I:%M %p")
    head = f"{label.capitalize()} ({day.strftime('%a %b %-d')}): "
    if not evs:
        reply.say(head + "nothing on your selected calendars.")
    else:
        reply.say(head + f"{len(evs)} event{'s' if len(evs) != 1 else ''}.")
        for e in evs:
            reply.say(f"  • {t(e)} — {e['summary']}" + (f" ({e['calendar']})" if e.get("calendar") else ""))
    wins = ctx.work_windows(evs, day, tz)
    if wins:
        longest = max(wins, key=lambda w: w["minutes"])
        reply.say(f"Longest open block: {datetime.fromisoformat(longest['start'].replace('Z', '+00:00')).astimezone(zi).strftime('%-I:%M %p')}"
                  f" ({longest['minutes'] // 60}h {longest['minutes'] % 60}m).")
    for c in ctx.conflicts(evs):
        reply.say(f"Conflict: {c}")
    dls = [d for d in ctx.deadlines_snapshot(conn, uid, day, 0)["items"] if d["local_date"] == day.isoformat()]
    for d in dls:
        reply.say(f"  ⚑ Deadline: {d['summary']}")
    timed = [e for e in evs if not e["all_day"]]
    if not evs:
        reply.spoken = f"{label.capitalize()} your calendar is clear."
    else:
        first = timed[0] if timed else evs[0]
        reply.spoken = (f"{label.capitalize()} you have {len(evs)} event{'s' if len(evs) != 1 else ''}. "
                        f"First up: {first['summary']}" + (f" at {t(first)}" if not first["all_day"] else "") + ".")


def _weather(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str) -> None:
    from jarvis.agents import weather as W
    if not W.enabled(conn, inp.user_id):
        reply.say("Weather is turned off in Settings.")
        return
    loc = W.location(conn, inp.user_id)
    day = date.fromisoformat(it.new_when.date) if it.new_when and it.new_when.date else ctx.local_today(conn, inp.user_id)
    run_event(conn, run_id, "tool", f"Checking the forecast for {loc['name']} (Open-Meteo)")
    try:
        data = W.fetch(loc, inp.tz)
    except W.WeatherUnavailable as e:
        reply.say(str(e) + " Try again in a moment.")
        reply.spoken = "I couldn't reach the weather service."
        return
    f = W.forecast_for(data, day)
    from jarvis.agents import visuals as V
    reply.visuals.append(V.weather(data, loc, day))
    if not f:
        reply.say(f"I only have a 7-day forecast; {day.strftime('%a %b %-d')} is outside it.")
        return
    text, spoken = W.describe(f, loc["name"], _spoken_date(day.isoformat(), inp.tz))
    reply.say(text)
    reply.spoken = spoken


def speechify(text: str) -> str:
    """Make text safe to read aloud: no markdown symbols, bullets, arrows, URLs or IDs are ever spoken."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"\[[^\]]*·[^\]]*\]", "", t)             # [source · locator] citations
    t = t.replace(" / ", ", ")
    t = re.sub(r"`{1,3}[^`]*`{1,3}", lambda m: m.group(0).strip("`"), t)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)          # **bold** / __bold__
    t = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"\1", t)   # *italic*
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)      # headings
    t = re.sub(r"^\s*(?:[-*+•▸⚑]|\d+[.)])\s+", "", t, flags=re.M)   # list markers
    t = re.sub(r"[*#_`|>~▸⚑•→—–]+", " ", t)             # any leftover symbols
    t = t.replace("°F", " degrees").replace("%", " percent")
    t = re.sub(r"\s*\n\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip(" .") + ("." if t.strip() else "")


def _spoken(text: str) -> str:
    first = [ln.strip(" •") for ln in text.splitlines() if ln.strip()][:3]
    s = " ".join(first)
    s = re.sub(r"https?://\S+", "", s)
    return s[:320]


# ---------------------------------------------------------------- dispatch
def _dispatch(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str, active: dict | None,
              captured: datetime, tv: dict | None, specialists: list[str]) -> None:
    intent = it.intent
    if intent == "cancel":
        return _cancel(conn, inp, reply, run_id)
    if intent in ("approve", "vague_consent"):
        return _voice_approve(conn, inp, it, reply, run_id, active, tv)
    if intent in ("correct", "remove_item"):
        specialists.append("tasks")
        return _correct(conn, inp, it, reply, run_id, active, captured)
    if intent == "retrieve_actions":
        return _retrieve_actions(conn, inp, reply)
    if intent == "remember":
        return _remember(conn, inp, it, reply, run_id)
    if intent == "forget_memory":
        reply.say("To forget a memory, open Memory and choose Forget on the exact record — I won't guess which one.")
        return None
    if intent in ("brief", "debrief", "war_room"):
        specialists.append("briefing")
        from jarvis.briefings import workflows
        kind = {"brief": "daily_command", "debrief": "nightly_debrief", "war_room": "weekly_war_room"}[intent]
        run_event(conn, run_id, "tool", "Reading selected calendars, tasks and deadlines")
        b = workflows.generate(conn, inp.user_id, kind, force=False, run_id=run_id)
        reply.say(workflows.render_text(b))
        reply.spoken = b["spoken_summary"]
        return None
    if intent == "gta_queue":
        specialists.append("school_gta")
        run_event(conn, run_id, "tool", "Reading GTA obligations, school tasks and deadlines")
        r = S.gta_queue(conn, inp.user_id)
        reply.say(r["text"])
        if r.get("basis"):
            reply.say(f"(Using your preference: {r['basis']})")
        reply.spoken = r["spoken"]
        return None
    if intent == "schedule":
        specialists.append("calendar")
        return _schedule(conn, inp, it, reply, run_id)
    if intent == "complete":
        specialists.append("tasks")
        run_event(conn, run_id, "tool", "Reading open Todoist tasks")
        r = S.propose_completion(conn, inp.user_id, inp.text, inp.tz)
        reply.say(r["message"])
        reply.questions += r.get("questions", [])
        if r["specs"]:
            _new_proposal(conn, inp, reply, run_id, "Complete task", r["specs"], r["message"], [], [])
        return None
    if intent == "capture":
        specialists.append("tasks")
        return _capture(conn, inp, it.commitments, reply, run_id)
    if intent == "meeting_debrief":
        specialists.append("meeting_debrief")
        from jarvis.agents import meeting
        meeting.debrief(conn, inp, reply, run_id)
        return None
    if intent == "area_summary":
        specialists.append(it.payload)
        run_event(conn, run_id, "tool", f"Reading current {it.payload} records and tasks")
        reply.say(S.area_summary(conn, inp.user_id, it.payload))
        snap = ctx.tasks_snapshot(conn, inp.user_id)
        from jarvis.agents.interpret import detect_area
        rel = [t["content"] for t in snap["tasks"] if detect_area(t["content"]) == it.payload][:5]
        reply.say("Open tasks: " + ("; ".join(rel) if rel else ("none" if snap["status"] == "ok" else f"Todoist {snap['status']}")))
        if (plan := ctx.current_plan(conn, inp.user_id, ctx.local_today(conn, inp.user_id))):
            reply.say(f"(Today's mission: {plan['mission']})")
        reply.spoken = f"{len(rel)} open {it.payload} tasks." if snap["status"] == "ok" else "Todoist isn't connected."
        return None
    if intent == "agenda":
        specialists.append("calendar")
        return _agenda(conn, inp, it, reply, run_id)
    if intent == "weather":
        specialists.append("weather")
        return _weather(conn, inp, it, reply, run_id)
    if intent == "local_file":
        specialists.append("local_operator")
        from jarvis.localops import operator
        operator.handle(conn, inp, reply, run_id)
        return None
    if intent in ("knowledge", "summarize"):
        specialists.append("knowledge")
        from jarvis.agents import knowledge_agent
        knowledge_agent.answer(conn, inp, reply, run_id)
        return None
    specialists.append("chief")
    return _question(conn, inp, reply, run_id)


def _new_proposal(conn, inp: CommandInput, reply: Reply, run_id: str, title: str, specs, summary: str, assumptions, questions,
                  origin: str | None = None):
    origin = origin if origin not in (None, "command") else inp.origin
    if reply.defer:
        reply.deferred.append((title, specs, summary, assumptions, questions))
        return None
    if (old := P.active_for_conversation(conn, inp.user_id, inp.conversation_id)):
        # one active approval context: an older unapproved plan is superseded, never silently executed
        conn.execute("UPDATE proposals SET status='superseded', updated_at=? WHERE id=?", (now_iso(), old["id"]))
        run_event(conn, run_id, "status", "Previous unapproved plan superseded by this one")
    pid = P.create(conn, inp.user_id, title, specs, summary, conversation_id=inp.conversation_id, run_id=run_id, capture_id=inp.capture_id,
                   assumptions=assumptions, questions=questions, origin=origin,
                   source={"capture_id": inp.capture_id, "text": inp.text, "captured_at": inp.captured_at, "tz": inp.tz})
    reply.proposal_id = pid
    from jarvis.actions import policy
    auto = policy.try_auto_approve(conn, pid)
    if auto:
        rules = ", ".join(f"“{x}”" for x in auto["labels"])
        reply.say(f"Approved automatically by your rule {rules}. You can undo it for the next five minutes.")
        run_event(conn, run_id, "approval", f"Auto-approved by rule {rules}")
    else:
        run_event(conn, run_id, "approval", "Awaiting your approval")
    return pid


def _capture(conn, inp: CommandInput, commitments: list[Commitment], reply: Reply, run_id: str) -> None:
    run_event(conn, run_id, "tool", f"Separated {len(commitments)} commitment(s); checking for duplicates")
    specs, assumptions, questions = S.propose_tasks(conn, inp.user_id, commitments, inp.tz)
    lines = []
    for i, (c, s) in enumerate(zip(commitments, specs)):
        when = s.args.get("due_local") or s.args.get("due_date")
        pod = f", {c.when.part_of_day}" if c.when.part_of_day else ""
        lines.append(f"{i + 1}. {c.content}" + (f" — {_fmt_date(when)}{pod}" if when else " — no date"))
    blocked = sorted({m for s in specs for m in s.missing_fields})
    reply.say(f"{len(specs)} task{'s' if len(specs) != 1 else ''} ready for Todoist:")
    for ln in lines:
        reply.say(ln)
    if blocked:
        reply.say("Before I can add them: " + "; ".join(blocked) + ".")
    reply.questions += questions
    n = len(specs)
    said = "; ".join(f"{c.content}, {_spoken_date(s.args.get('due_date') or (s.args.get('due_local') or '')[:10], inp.tz)}"
                     if (s.args.get('due_date') or s.args.get('due_local')) else c.content for c, s in zip(commitments, specs))
    head = f"I have {n} task{'s' if n != 1 else ''} ready: {said}."
    if not blocked:
        reply.spoken = head + (" Shall I add them?" if n > 1 else " Shall I add it?")
    elif "not connected" in blocked[0].lower():
        reply.spoken = head + " One thing first: Todoist isn't connected yet."
    else:
        reply.spoken = head + " One thing first: pick which Todoist project I should use, right on the card."
    _new_proposal(conn, inp, reply, run_id, f"{len(specs)} task(s) from your request", specs,
                  "; ".join(ln.split('. ', 1)[1] for ln in lines), assumptions, questions)


def _spoken_date(iso_date: str, tz: str = "America/Chicago") -> str:
    try:
        d = date.fromisoformat(iso_date[:10])
    except ValueError:
        return iso_date
    from zoneinfo import ZoneInfo

    from jarvis.core.util import utcnow
    today = utcnow().astimezone(ZoneInfo(tz)).date()
    if d == today:
        return "today"
    if (d - today).days == 1:
        return "tomorrow"
    return d.strftime("%A") if 0 < (d - today).days < 7 else d.strftime("%A, %B %-d")


def _fmt_date(s: str) -> str:
    try:
        if " " in s:
            d, t, _ = s.split(" ", 2)
            return datetime.fromisoformat(f"{d}T{t}").strftime("%a %b %d %-I:%M %p")
        return date.fromisoformat(s).strftime("%a %b %d")
    except ValueError:
        return s


def _schedule(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str) -> None:
    if not it.commitments:
        reply.say("What should I block time for, how long, and which day?")
        reply.questions.append("What, how long, and which day?")
        return
    c = it.commitments[0]
    run_event(conn, run_id, "tool", "Reading selected calendars for free time")
    r = S.propose_event(conn, inp.user_id, c, inp.tz)
    if (plan := ctx.current_plan(conn, inp.user_id, ctx.local_today(conn, inp.user_id))):
        reply.say(f"(Today's mission: {plan['mission']})")
    reply.say(r["message"])
    reply.questions += r["questions"]
    if r["specs"]:
        _new_proposal(conn, inp, reply, run_id, f"Calendar block: {r['specs'][0].args['summary']}", r["specs"], r["message"],
                      r["assumptions"], [])
        reply.spoken = r["message"] + " Approve?"


def _cancel(conn, inp: CommandInput, reply: Reply, run_id: str) -> None:
    uid = inp.user_id
    latest = P.latest_for_conversation(conn, uid, inp.conversation_id)
    if not latest or latest["status"] in ("rejected", "expired", "superseded", "cancelled", "verified"):
        reply.say("Nothing pending to cancel.")
        return
    try:
        P.reject(conn, uid, latest["id"], actor="user")
        reply.say("Cancelled — that plan won't run. Nothing was written.")
        run_event(conn, run_id, "status", "Plan cancelled before execution")
    except P.ProposalError as e:
        v = P.view(conn, uid, latest["id"])
        done = [i for i in v["items"] if i["state"] in ("verified", "executing", "unknown")]
        reply.say(e.message + " Current state: " + "; ".join(f"{i['index'] + 1}. {i['state']}" for i in done) + ".")
        reply.say("Queued items that hadn't started were stopped. Say 'undo' with the item if you want a compensating action proposed.")
        with tx(conn):
            for i in v["items"]:
                if i["state"] == "queued":
                    conn.execute("UPDATE proposed_actions SET state='cancelled', updated_at=? WHERE id=?", (now_iso(), i["action_id"]))
                    conn.execute("UPDATE jobs SET status='cancelled' WHERE kind='execute_action' AND status='queued' AND "
                                 "json_extract(payload,'$.action_id')=?", (i["action_id"],))
            P.recompute_status(conn, latest["id"])


def _voice_approve(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str, active: dict | None, tv: dict | None):
    if it.intent == "vague_consent":
        reply.say("To run it, say “approve” (or “approve 1 and 3”), or tap Approve on the card.")
        return
    if not active:
        reply.say("There's no plan waiting for approval in this conversation.")
        return
    if inp.speech_playing:
        reply.say("I heard “approve” while I was speaking, so I didn't treat it as approval. Tap Approve or say it again.")
        audit(conn, "system", "approval.refused", "Approval heard during TTS playback ignored", "proposal", active["id"],
              uid=inp.user_id)
        return
    if inp.channel == "voice" and tv and tv["source"] == "stt":
        min_conf = get_settings().stt_min_confidence
        if tv["confidence"] is None or tv["confidence"] < min_conf:
            reply.say("I'm not confident I heard that clearly, so I didn't approve anything. Tap Approve or repeat it.")
            return
    v = P.view(conn, inp.user_id, active["id"])
    sel = [i["index"] for i in v["items"] if i["state"] == "proposed"] if it.approve_all else \
        [r if r >= 0 else len(v["items"]) - 1 for r in it.item_refs]
    try:
        P.approve(conn, inp.user_id, active["id"], v["version"], v["hash"], sel, channel="voice" if inp.channel == "voice" else "typed",
                  session_id=inp.session_id, evidence=inp.text)
    except P.ProposalError as e:
        reply.say(e.message)
        return
    reply.proposal_id = None
    reply.say(f"Approved {len(sel)} item(s) of version {v['version']}. Executing and verifying with the provider now.")
    reply.spoken = "Very good. Executing now — I'll confirm once the provider has it."
    run_event(conn, run_id, "approval", f"Approved via {inp.channel}: items {[s + 1 for s in sel]}")
    conn.execute("UPDATE messages SET proposal_id=? WHERE run_id=? AND role='user'", (active["id"], run_id))


def _match_item(v: dict, it: Interpretation) -> tuple[list[int], str | None]:
    items = [i for i in v["items"] if i["state"] == "proposed"]
    if it.item_refs:
        idx = [r if r >= 0 else len(v["items"]) - 1 for r in it.item_refs]
        return [i for i in idx if 0 <= i < len(v["items"])], None
    if it.old_phrase:
        hits = [i["index"] for i in items if re.search(rf"\b{re.escape(it.old_phrase.lower())}\b", (i["reason"] or "").lower())]
        if len(hits) == 1:
            return hits, None
    if it.ref_text:
        scored = sorted(((memory.similarity(i["args"].get("content") or i["args"].get("summary") or "", it.ref_text), i["index"])
                         for i in items), reverse=True)
        if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return [scored[0][1]], None
    if len(items) == 1:
        return [items[0]["index"]], None
    names = "; ".join(f"{i['index'] + 1}) {i['args'].get('content') or i['args'].get('summary')}" for i in items)
    return [], f"Which one should change? {names}"


def _correct(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str, active: dict, captured: datetime):
    uid = inp.user_id
    v = P.view(conn, uid, active["id"])
    idxs, question = _match_item(v, it)
    if question:
        reply.say(question)
        reply.questions.append(question)
        return
    pv = P.current_version(conn, uid, active["id"])
    specs = P.specs_from_actions(P.actions_for_version(conn, pv["pv"]["id"]))
    tz = jload(pv["pv"]["source"], {}).get("tz", inp.tz)
    changes = []
    if it.intent == "remove_item":
        keep = [s for i, s in enumerate(specs) if i not in idxs]
        if not keep:
            P.reject(conn, uid, active["id"])
            reply.say("Removed the only item — nothing left to approve.")
            return
        changes = [f"removed item {i + 1}" for i in idxs]
        specs = keep
    else:
        nw = it.new_when   # resolved against this correction's own capture time
        for i in idxs:
            s = specs[i]
            if s.action_type == "task.create":
                for k in ("due_date", "due_datetime", "due_local", "duration", "duration_unit"):
                    s.args.pop(k, None)
                if nw.date and nw.time:
                    s.args["due_datetime"] = timeparse.to_utc_instant(nw.date, nw.time, tz).strftime("%Y-%m-%dT%H:%M:%SZ")
                    s.args["due_local"] = f"{nw.date} {nw.time} {tz}"
                elif nw.date:
                    s.args["due_date"] = nw.date
                s.after = {"content": s.args["content"], "due": s.args.get("due_local") or s.args.get("due_date")}
                label = s.args["content"]
                due_txt = s.args.get("due_local") or s.args.get("due_date")
                tgt = f" in {s.target_name}" if s.target_name else ""
                s.expected_effect = f"Create Todoist task “{label}”" + (f" due {due_txt}" if due_txt else "") + tgt
                changes.append(f"“{label}” → {_fmt_date(s.args.get('due_local') or s.args.get('due_date'))}")
            elif s.action_type == "event.create":
                changes.append("calendar blocks must be re-proposed with a fresh free-time check")
    new_v = P.revise(conn, uid, active["id"], specs, "; ".join(changes), actor="user")
    reply.proposal_id = active["id"]
    reply.say(f"Updated (version {new_v}): " + "; ".join(changes) + ". The earlier version can no longer be approved.")
    reply.spoken = "Updated. " + "; ".join(changes) + ". Shall I proceed?"
    run_event(conn, run_id, "status", f"Plan revised to v{new_v}; prior approval voided")


def _retrieve_actions(conn, inp: CommandInput, reply: Reply) -> None:
    uid = inp.user_id
    props = rows(conn, "SELECT id FROM proposals WHERE user_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 3",
                 (uid, inp.conversation_id))
    if not props:
        props = rows(conn, "SELECT id FROM proposals WHERE user_id=? ORDER BY updated_at DESC LIMIT 1", (uid,))
    for p in props:
        v = P.view(conn, uid, p["id"])
        done = [i for i in v["items"] if i["state"] not in ("proposed", "superseded", "skipped")]
        if not done and v["status"] == "proposed":
            continue
        reply.say(f"{v['title']} (v{v['version']}, {v['status']}):")
        for i in v["items"]:
            if i["state"] == "skipped":
                continue
            rc = i["receipt"]
            how = f"verified via {rc['method']}{' [SIMULATED]' if rc and rc['simulated'] else ''}" if rc and rc["ok"] else i["state"]
            reply.say(f"  {i['index'] + 1}. {i['expected_effect']} — {how}" + (f" {i['external_url']}" if i["external_url"] else ""))
        reply.spoken = f"{v['title']}: {v['status']}."
        return
    active = P.active_for_conversation(conn, uid, inp.conversation_id)
    reply.say("Nothing has been added yet." + (" One plan is still waiting for your approval." if active else ""))


def _remember(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str) -> None:
    content = it.payload.strip().rstrip(".")
    if not content:
        reply.say("What should I remember?")
        return
    content = content[0].upper() + content[1:]
    from jarvis.agents.interpret import detect_area
    area = detect_area(content)
    proj = row(conn, "SELECT id FROM projects WHERE area=? AND user_id=?", (area, inp.user_id)) if area else None
    res = memory.add(conn, inp.user_id, content, "preference", "user_statement", source_ref=f"capture:{inp.capture_id}",
                     project_id=proj["id"] if proj else None)
    run_event(conn, run_id, "tool", "Saved to memory")
    reply.say(f"Saved as a preference: “{content}”.")
    if res["superseded"]:
        old = [memory.get(conn, inp.user_id, m, with_versions=False)["content"] for m in res["superseded"]]
        reply.say("This replaces: " + " | ".join(f"“{o}”" for o in old))
    reply.spoken = "Noted. I'll remember that." + (" It replaces your earlier preference." if res["superseded"] else "")


def _question(conn, inp: CommandInput, reply: Reply, run_id: str) -> None:
    from jarvis.llm.provider import LLMError, get_provider
    llm = get_provider()
    ok, why = llm.available(conn)
    if not ok:
        reply.say("I can capture tasks, correct them, block calendar time, brief you, search your sources and remember "
                  f"preferences without the model. Open-ended answers need model inference, which is off: {why}")
        reply.spoken = "Open-ended answers need the model, which is off right now."
        return
    tz = inp.tz
    snap = ctx.today(conn, inp.user_id)
    mems = memory.relevant(conn, inp.user_id, inp.text, limit=6)
    plan = snap["plan"]
    context_lines = [f"Now: {inp.captured_at} ({tz}). Today {snap['date']}."]
    if plan:
        context_lines.append(f"Current daily mission (do not invent another): {plan['mission']}; priorities: {plan['priorities']}")
    for k, v in snap["sources"].items():
        context_lines.append(f"Source {k}: {v['status']}")
    context_lines += [f"Agenda: {e['summary']} {e['start']}" for e in snap["agenda"][:10]]
    context_lines += [f"Due: {t['content']}" for t in (snap["tasks_due"] + snap["overdue"])[:10]]
    context_lines += [f"Memory ({m['source_kind']}, {m['observed_at'][:10]}): {m['content']}" for m in mems]
    run_event(conn, run_id, "tool", "Asking the reasoning model with bounded context")
    from jarvis.agents.persona import persona
    me = persona(conn, inp.user_id)
    system = (f"You are {me.assistant}, {me.name}'s concise personal operator. Lead with the priority and next action. Use "
              "only the provided context; say when data is missing or a source is not connected. You cannot take actions "
              f"from this answer; if an action is needed, tell {me.name} what to say to propose it. Keep it under 120 words. "
              "Write plain conversational sentences only: no markdown, no asterisks, no bullet symbols, no headings — your "
              "reply is also read aloud.")
    try:
        out = llm.complete(conn, uid=inp.user_id, purpose="question", system=system,
                           content=[{"type": "text", "text": "Context:\n" + "\n".join(context_lines)},
                                    {"type": "text", "text": f"{me.name} asks: " + inp.text}], max_tokens=800, run_id=run_id)
        reply.say(out["text"].strip())
    except LLMError as e:
        reply.say(f"Model unavailable ({e.kind}): {e.message}")
        reply.status = "failed" if e.kind not in ("budget",) else "completed"
