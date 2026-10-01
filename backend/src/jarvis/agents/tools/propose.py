"""Proposal tools: they only append ActionSpecs to the run. Nothing is written until the proposal is approved."""
from __future__ import annotations

import re

from jarvis.agents import specialists as S
from jarvis.agents.interpret import Commitment, detect_area
from jarvis.agents.tools import obj, tool
from jarvis.core.util import run_event

PARTS = {"type": "string", "enum": ["morning", "afternoon", "evening"]}


@tool("propose_tasks",
      "PROPOSE new Todoist tasks. Only when AJ asked to add/track/remind something. Creates an approval card; "
      "nothing is written until AJ approves. `when_phrase` MUST be copied verbatim from AJ's own words "
      "(e.g. 'tomorrow', 'Friday at 3pm'), or omitted if AJ gave no timing — never invent dates.",
      obj({"tasks": {"type": "array", "minItems": 1, "maxItems": 10, "items": obj({
          "content": {"type": "string", "description": "Short task title in AJ's terms"},
          "when_phrase": {"type": "string"},
          "note": {"type": "string"}}, ["content"])}}, ["tasks"]),
      risk="propose", family="tasks", manual="Prepares Todoist tasks on an approval card.",
      example="Add a task to email Dr. Smith on Friday.")
def propose_tasks(run, tasks: list[dict]):
    cs: list[Commitment] = []
    rejected = []
    for t in tasks:
        w = run.when(t.get("when_phrase"))
        if isinstance(w, dict):
            rejected.append({"content": t.get("content"), **w})
            continue
        content = t["content"].strip().rstrip(".")
        content = content[:1].upper() + content[1:]
        cs.append(Commitment(text=f"{content} {t.get('when_phrase') or ''}".strip(), content=content, when=w,
                             area=detect_area(content + " " + run.said)))
    if cs:
        specs, assumptions, _ = S.propose_tasks(run.conn, cs, run.tz)
        for sp, t in zip(specs, [t for t in tasks if not any(r.get("content") == t.get("content") for r in rejected)]):
            if t.get("note"):
                sp.args["description"] = ((sp.args.get("description") or "") + "\n" + t["note"]).strip()
        run.specs += specs
        run.assumptions += assumptions
        run_event(run.conn, run.run_id, "tool", f"Prepared {len(specs)} task(s) for your approval")
    return {"proposed": [{"content": s.args["content"], "due": s.args.get("due_local") or s.args.get("due_date"),
                          "blocked_by": s.missing_fields} for s in run.specs[-len(cs):]] if cs else [],
            "rejected": rejected, "status": "awaiting AJ's approval (not yet in Todoist)"}


@tool("propose_calendar_block",
      "PROPOSE a personal calendar block (no attendees). Finds a free slot unless an exact time was given. "
      "Only when AJ asked to block/schedule time. `when_phrase` must be copied verbatim from AJ's words.",
      obj({"summary": {"type": "string"}, "when_phrase": {"type": "string"},
           "minutes": {"type": "integer", "minimum": 5, "maximum": 480}, "part_of_day": PARTS},
          ["summary", "when_phrase", "minutes"]),
      risk="propose", family="calendar", manual="Finds a free slot and prepares a personal calendar block for approval.",
      example="Block two hours tomorrow morning for thesis writing.")
def propose_calendar_block(run, summary: str, when_phrase: str, minutes: int, part_of_day: str | None = None):
    w = run.when(when_phrase)
    if isinstance(w, dict):
        return w
    if not w.date:
        return {"error": "No day in AJ's words; ask him which day."}
    w.duration_min = minutes
    if part_of_day and not w.part_of_day:
        w.part_of_day = part_of_day
    c = Commitment(text=f"{summary} {when_phrase}", content=summary, when=w, area=detect_area(summary), kind="event")
    r = S.propose_event(run.conn, c, run.tz)
    run.specs += r["specs"]
    run.assumptions += r["assumptions"]
    run_event(run.conn, run.run_id, "tool", "Checked free time and prepared a calendar block" if r["specs"]
              else "Couldn't find a valid slot")
    return {"result": r["message"], "proposed": bool(r["specs"]), "questions": r["questions"],
            "status": "awaiting AJ's approval" if r["specs"] else "not proposed"}


@tool("propose_complete_task", "PROPOSE marking an existing Todoist task complete (AJ said he finished it).",
      obj({"task_query": {"type": "string"}}, ["task_query"]), risk="propose", family="tasks",
      example="I finished the lab report.")
def propose_complete_task(run, task_query: str):
    r = S.propose_completion(run.conn, task_query, run.tz)
    run.specs += r["specs"]
    return {"result": r["message"], "proposed": bool(r["specs"])}


@tool("propose_note", "PROPOSE writing a Markdown note into AJ's workspace folder (preview + approval required).",
      obj({"title": {"type": "string"}, "content": {"type": "string"}}, ["title", "content"]),
      risk="propose", family="local_operator", example="Write a note summarizing today's meeting.")
def propose_note(run, title: str, content: str):
    from jarvis.actions.proposals import ActionSpec
    from jarvis.localops import files as F
    roots = [r for r in F.list_roots(run.conn) if r["writable"]]
    if not roots:
        return {"error": "No writable workspace folder designated (Settings → Local files)."}
    safe = re.sub(r"[^\w\- ]+", "", title).strip() or "note"
    try:
        plan = F.plan_write(run.conn, roots[0]["id"], f"Notes/{safe}.md", f"# {title}\n\n{content}\n")
    except F.FileOpError as e:
        return {"error": e.message}
    run.specs.append(ActionSpec(action_type="file.write", args=plan["args"], timezone=run.tz, target_resource=roots[0]["id"],
                                target_name=roots[0]["label"], before=plan["before"],
                                after=plan["after"] | {"diff": plan["diff"]},
                                expected_effect=f"Create {roots[0]['label']}/Notes/{safe}.md", reason="Requested by AJ"))
    return {"proposed": True, "status": "awaiting AJ's approval"}
