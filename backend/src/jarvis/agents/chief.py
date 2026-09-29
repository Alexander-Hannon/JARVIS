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
from jarvis.agents.interpret import Commitment, Interpretation, classify
from jarvis.config import get_settings
from jarvis.core import timeparse
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, new_id, now_iso, parse_iso, record_latency, run_event
from jarvis.memory import service as memory

log = logging.getLogger("jarvis.chief")
RUN_LIMITS = {"max_specialists": 3, "max_model_calls": 3, "max_seconds": 90}


@dataclass
class CommandInput:
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


class Reply:
    def __init__(self):
        self.text_lines: list[str] = []
        self.spoken: str | None = None
        self.proposal_id: str | None = None
        self.questions: list[str] = []
        self.citations: list[dict] = []
        self.status = "completed"

    def say(self, s: str):
        self.text_lines.append(s)


def ensure_conversation(conn, conversation_id: str | None) -> str:
    if conversation_id and row(conn, "SELECT id FROM conversations WHERE id=?", (conversation_id,)):
        return conversation_id
    cid = conversation_id if conversation_id and re.fullmatch(r"conv_[a-f0-9]{20}", conversation_id) else new_id("conv")
    conn.execute("INSERT INTO conversations (id, created_at, updated_at) VALUES (?,?,?)", (cid, now_iso(), now_iso()))
    return cid


def _typed_capture(conn, inp: CommandInput) -> str:
    cid = new_id("cap")
    conn.execute("INSERT INTO captures (id, conversation_id, kind, captured_at, capture_tz, status, created_at) "
                 "VALUES (?,?,?,?,?, 'transcribed', ?)", (cid, inp.conversation_id, "text", inp.captured_at, inp.tz, now_iso()))
    conn.execute("INSERT INTO transcript_versions (id, capture_id, version, text, source, created_at) VALUES (?,?,1,?, 'typed', ?)",
                 (new_id("tv"), cid, inp.text, now_iso()))
    return cid


def handle(conn: sqlite3.Connection, inp: CommandInput) -> dict:
    t0 = time.perf_counter()
    inp.conversation_id = ensure_conversation(conn, inp.conversation_id)
    tv = None
    if inp.capture_id:
        cap = row(conn, "SELECT * FROM captures WHERE id=?", (inp.capture_id,))
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

    run_id = new_id("run")
    conn.execute("INSERT INTO runs (id, conversation_id, capture_id, kind, status, limits, started_at) VALUES (?,?,?,?,?,?,?)",
                 (run_id, inp.conversation_id, inp.capture_id, "command", "running", jdump(RUN_LIMITS), now_iso()))
    conn.execute("INSERT INTO messages (id, conversation_id, role, text, capture_id, run_id, created_at) VALUES (?,?,?,?,?,?,?)",
                 (new_id("msg"), inp.conversation_id, "user", inp.text, inp.capture_id, run_id, now_iso()))
    active = P.active_for_conversation(conn, inp.conversation_id)
    captured = parse_iso(inp.captured_at)
    interp = classify(inp.text, has_active_proposal=bool(active), has_attachments=bool(inp.attachments),
                      captured_at=captured, tz=inp.tz)
    run_event(conn, run_id, "route", f"Understood as: {interp.intent.replace('_', ' ')}")
    reply = Reply()
    specialists: list[str] = []
    try:
        _dispatch(conn, inp, interp, reply, run_id, active, captured, tv, specialists)
    except Exception as e:  # visible, recoverable failure — never fabricated success
        log.exception("command failed")
        reply.say(f"Something failed while handling that ({type(e).__name__}). Nothing was changed.")
        reply.status = "failed"
        run_event(conn, run_id, "error", f"Run failed: {type(e).__name__}")
    if reply.proposal_id:
        P.mark_presented(conn, reply.proposal_id)
    text = "\n".join(reply.text_lines).strip()
    spoken = reply.spoken or _spoken(text)
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
            "proposal": P.view(conn, reply.proposal_id) if reply.proposal_id else None, "questions": reply.questions,
            "citations": reply.citations, "events": events}
    conn.execute("UPDATE runs SET status=?, intent=?, specialists=?, finished_at=? WHERE id=?",
                 (status, jdump({"intent": interp.intent, "tv": tv["version"] if tv else 1, "response": resp}),
                  jdump(specialists), now_iso(), run_id))
    conn.execute("UPDATE conversations SET updated_at=?, title=COALESCE(title, ?) WHERE id=?",
                 (now_iso(), inp.text[:60], inp.conversation_id))
    record_latency(conn, "first_response", (time.perf_counter() - t0) * 1000, {"intent": interp.intent})
    if reply.proposal_id:
        record_latency(conn, "proposal", (time.perf_counter() - t0) * 1000)
    return resp


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
        b = workflows.generate(conn, kind, force=False, run_id=run_id)
        reply.say(workflows.render_text(b))
        reply.spoken = b["spoken_summary"]
        return None
    if intent == "gta_queue":
        specialists.append("school_gta")
        run_event(conn, run_id, "tool", "Reading GTA obligations, school tasks and deadlines")
        r = S.gta_queue(conn)
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
        r = S.propose_completion(conn, inp.text, inp.tz)
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
        reply.say(S.area_summary(conn, it.payload))
        snap = ctx.tasks_snapshot(conn)
        from jarvis.agents.interpret import detect_area
        rel = [t["content"] for t in snap["tasks"] if detect_area(t["content"]) == it.payload][:5]
        reply.say("Open tasks: " + ("; ".join(rel) if rel else ("none" if snap["status"] == "ok" else f"Todoist {snap['status']}")))
        if (plan := ctx.current_plan(conn, ctx.local_today(conn))):
            reply.say(f"(Today's mission: {plan['mission']})")
        reply.spoken = f"{len(rel)} open {it.payload} tasks." if snap["status"] == "ok" else "Todoist isn't connected."
        return None
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


def _new_proposal(conn, inp: CommandInput, reply: Reply, run_id: str, title: str, specs, summary: str, assumptions, questions):
    if (old := P.active_for_conversation(conn, inp.conversation_id)):
        # one active approval context: an older unapproved plan is superseded, never silently executed
        conn.execute("UPDATE proposals SET status='superseded', updated_at=? WHERE id=?", (now_iso(), old["id"]))
        run_event(conn, run_id, "status", "Previous unapproved plan superseded by this one")
    pid = P.create(conn, title, specs, summary, conversation_id=inp.conversation_id, run_id=run_id, capture_id=inp.capture_id,
                   assumptions=assumptions, questions=questions, source={"capture_id": inp.capture_id, "text": inp.text,
                                                                           "captured_at": inp.captured_at, "tz": inp.tz})
    reply.proposal_id = pid
    run_event(conn, run_id, "approval", "Awaiting your approval")
    return pid


def _capture(conn, inp: CommandInput, commitments: list[Commitment], reply: Reply, run_id: str) -> None:
    run_event(conn, run_id, "tool", f"Separated {len(commitments)} commitment(s); checking for duplicates")
    specs, assumptions, questions = S.propose_tasks(conn, commitments, inp.tz)
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
    reply.spoken = head + (" Shall I add them?" if not blocked else " One thing first — " + blocked[0].split(" (")[0].lower() + ".")
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
    r = S.propose_event(conn, c, inp.tz)
    if (plan := ctx.current_plan(conn, ctx.local_today(conn))):
        reply.say(f"(Today's mission: {plan['mission']})")
    reply.say(r["message"])
    reply.questions += r["questions"]
    if r["specs"]:
        _new_proposal(conn, inp, reply, run_id, f"Calendar block: {r['specs'][0].args['summary']}", r["specs"], r["message"],
                      r["assumptions"], [])
        reply.spoken = r["message"] + " Approve?"


def _cancel(conn, inp: CommandInput, reply: Reply, run_id: str) -> None:
    latest = P.latest_for_conversation(conn, inp.conversation_id)
    if not latest or latest["status"] in ("rejected", "expired", "superseded", "cancelled", "verified"):
        reply.say("Nothing pending to cancel.")
        return
    try:
        P.reject(conn, latest["id"], actor="user")
        reply.say("Cancelled — that plan won't run. Nothing was written.")
        run_event(conn, run_id, "status", "Plan cancelled before execution")
    except P.ProposalError as e:
        v = P.view(conn, latest["id"])
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
        audit(conn, "system", "approval.refused", "Approval heard during TTS playback ignored", "proposal", active["id"])
        return
    if inp.channel == "voice" and tv and tv["source"] == "stt":
        min_conf = get_settings().stt_min_confidence
        if tv["confidence"] is None or tv["confidence"] < min_conf:
            reply.say("I'm not confident I heard that clearly, so I didn't approve anything. Tap Approve or repeat it.")
            return
    v = P.view(conn, active["id"])
    sel = [i["index"] for i in v["items"] if i["state"] == "proposed"] if it.approve_all else \
        [r if r >= 0 else len(v["items"]) - 1 for r in it.item_refs]
    try:
        P.approve(conn, active["id"], v["version"], v["hash"], sel, channel="voice" if inp.channel == "voice" else "typed",
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
    v = P.view(conn, active["id"])
    idxs, question = _match_item(v, it)
    if question:
        reply.say(question)
        reply.questions.append(question)
        return
    pv = P.current_version(conn, active["id"])
    specs = P.specs_from_actions(P.actions_for_version(conn, pv["pv"]["id"]))
    tz = jload(pv["pv"]["source"], {}).get("tz", inp.tz)
    changes = []
    if it.intent == "remove_item":
        keep = [s for i, s in enumerate(specs) if i not in idxs]
        if not keep:
            P.reject(conn, active["id"])
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
    new_v = P.revise(conn, active["id"], specs, "; ".join(changes), actor="user")
    reply.proposal_id = active["id"]
    reply.say(f"Updated (version {new_v}): " + "; ".join(changes) + ". The earlier version can no longer be approved.")
    reply.spoken = "Updated. " + "; ".join(changes) + ". Shall I proceed?"
    run_event(conn, run_id, "status", f"Plan revised to v{new_v}; prior approval voided")


def _retrieve_actions(conn, inp: CommandInput, reply: Reply) -> None:
    props = rows(conn, "SELECT id FROM proposals WHERE conversation_id=? ORDER BY created_at DESC LIMIT 3", (inp.conversation_id,))
    if not props:
        props = rows(conn, "SELECT id FROM proposals ORDER BY updated_at DESC LIMIT 1")
    for p in props:
        v = P.view(conn, p["id"])
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
    active = P.active_for_conversation(conn, inp.conversation_id)
    reply.say("Nothing has been added yet." + (" One plan is still waiting for your approval." if active else ""))


def _remember(conn, inp: CommandInput, it: Interpretation, reply: Reply, run_id: str) -> None:
    content = it.payload.strip().rstrip(".")
    if not content:
        reply.say("What should I remember?")
        return
    content = content[0].upper() + content[1:]
    from jarvis.agents.interpret import detect_area
    area = detect_area(content)
    proj = row(conn, "SELECT id FROM projects WHERE area=?", (area,)) if area else None
    res = memory.add(conn, content, "preference", "user_statement", source_ref=f"capture:{inp.capture_id}",
                     project_id=proj["id"] if proj else None)
    run_event(conn, run_id, "tool", "Saved to memory")
    reply.say(f"Saved as a preference: “{content}”.")
    if res["superseded"]:
        old = [memory.get(conn, m, with_versions=False)["content"] for m in res["superseded"]]
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
    snap = ctx.today(conn)
    mems = memory.relevant(conn, inp.text, limit=6)
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
    system = ("You are Jarvis, AJ's concise personal operator. Lead with the priority and next action. Use only the provided "
              "context; say when data is missing or a source is not connected. You cannot take actions from this answer; "
              "if an action is needed, tell AJ what to say to propose it. Keep it under 120 words.")
    try:
        out = llm.complete(conn, purpose="question", system=system,
                           content=[{"type": "text", "text": "Context:\n" + "\n".join(context_lines)},
                                    {"type": "text", "text": "AJ asks: " + inp.text}], max_tokens=800, run_id=run_id)
        reply.say(out["text"].strip())
    except LLMError as e:
        reply.say(f"Model unavailable ({e.kind}): {e.message}")
        reply.status = "failed" if e.kind not in ("budget",) else "completed"
