"""Local Operator specialist: maps AJ's own words (never document content) to typed file tools.
Reads run immediately inside granted roots; writes/moves become proposals with an exact preview."""
from __future__ import annotations

import re

from jarvis.actions.proposals import ActionSpec
from jarvis.core.db import row, rows
from jarvis.core.util import run_event
from jarvis.localops import files as F


def _pick_root(conn, text: str) -> dict | None:
    roots = F.list_roots(conn)
    if not roots:
        return None
    low = text.lower()
    for r in roots:
        if r["label"].lower() in low:
            return r
    return roots[0]


def handle(conn, inp, reply, run_id: str) -> None:
    from jarvis.agents.chief import _new_proposal
    text = inp.text.strip()
    low = text.lower()
    root = _pick_root(conn, text)
    if not root:
        reply.say("No workspace folders are designated yet. Add one in Settings → Local workspace (read-only or read/write).")
        return
    try:
        if re.search(r"\b(list|show)( me)? (the )?files\b", low):
            m = re.search(r"\bin (?:the )?(?:folder )?[\"“]?([\w\-./ ]+?)[\"”]?\s*$", text)
            sub = m.group(1).strip() if m and m.group(1).lower() != root["label"].lower() else ""
            run_event(conn, run_id, "tool", f"Listing {root['label']}/{sub}")
            items = F.list_dir(root, sub)
            reply.say(f"{root['label']}/{sub}: " + (", ".join(("📁 " if i["dir"] else "") + i["name"] for i in items[:40]) or "(empty)"))
            return
        if re.search(r"\bread (the )?file\b", low):
            m = re.search(r"read (?:the )?file\s+[\"“]?(.+?)[\"”]?\s*$", text, re.IGNORECASE)
            r = F.read_file(root, m.group(1)) if m else None
            if not r:
                reply.say("Which file? Give its path inside the workspace.")
                return
            run_event(conn, run_id, "tool", f"Read {root['label']}/{r['path']}")
            reply.say(f"{r['path']} ({r['bytes']} bytes):\n{r['text'][:3000]}")
            reply.spoken = f"Here's {r['path']}."
            return
        if re.search(r"\bsearch (my )?(workspace|files)\b", low):
            q = re.sub(r"^.*?\b(?:for|about)\s+", "", text, flags=re.IGNORECASE).strip(" ?.")
            hits = F.search(root, q)
            reply.say(f"Matches for “{q}” in {root['label']}: " + ("; ".join(h["path"] for h in hits[:20]) or "none"))
            return
        m = re.search(r"\b(?:rename|move)(?: the)?(?: file)?\s+[\"“]?(.+?)[\"”]?\s+to\s+[\"“]?(.+?)[\"”]?\s*$", text, re.IGNORECASE)
        if m:
            plan = F.plan_move(conn, root["id"], m.group(1).strip(), m.group(2).strip())
            spec = ActionSpec(action_type="file.move", args=plan["args"], timezone=inp.tz, target_resource=root["id"],
                              target_name=root["label"], before=plan["before"], after=plan["after"],
                              expected_effect=f"Move {root['label']}/{plan['args']['src']} → {plan['args']['dst']}",
                              reason=f"You said: “{text}”")
            reply.say(f"Move {plan['args']['src']} → {plan['args']['dst']} in {root['label']}? (No overwrite; source hash checked.)")
            _new_proposal(conn, inp, reply, run_id, "Move file", [spec], spec.expected_effect, [], [])
            return
        content, rel, title = _content_for_write(conn, inp, text)
        if content is None:
            reply.say(rel)
            return
        overwrite = bool(re.search(r"\boverwrite\b", low))
        plan = F.plan_write(conn, root["id"], rel, content, allow_overwrite=overwrite)
        spec = ActionSpec(action_type="file.write", args=plan["args"], timezone=inp.tz, target_resource=root["id"],
                          target_name=root["label"], before=plan["before"], after=plan["after"] | {"diff": plan["diff"]},
                          expected_effect=f"{'Overwrite' if plan['before']['exists'] else 'Create'} {root['label']}/{rel}",
                          reason=f"You said: “{text}”")
        reply.say(f"{spec.expected_effect} — preview:\n{plan['diff'][:1500]}")
        reply.spoken = f"Ready to {'overwrite' if plan['before']['exists'] else 'create'} {rel}. Approve?"
        _new_proposal(conn, inp, reply, run_id, title, [spec], spec.expected_effect, [], [])
    except F.FileOpError as e:
        reply.say(f"Can't do that: {e.message}")


def _content_for_write(conn, inp, text: str) -> tuple[str | None, str, str]:
    low = text.lower()
    if re.search(r"\b(briefing|brief)\b", low):
        b = row(conn, "SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1")
        if not b:
            return None, "No briefing exists yet — ask me to brief you first.", ""
        from jarvis.briefings.workflows import _view, render_text
        v = _view(b)
        return (f"# {v['content']['headline']}\n\n" + render_text(v).replace("\n", "\n\n") + "\n",
                f"Jarvis Exports/{b['kind']}-{b['for_date']}.md", "Export briefing")
    if re.search(r"\bmemor(y|ies)\b", low):
        from jarvis.memory.service import export_markdown
        return export_markdown(conn), "Jarvis Exports/memory-export.md", "Export memories"
    if re.search(r"\baction list\b|\bactions\b", low):
        acts = rows(conn, "SELECT a.expected_effect, a.state, a.external_url FROM proposed_actions a "
                          "WHERE a.state NOT IN ('superseded','skipped') ORDER BY a.updated_at DESC LIMIT 50")
        md = "# Jarvis action list\n\n" + "\n".join(f"- [{'x' if a['state'] == 'verified' else ' '}] {a['expected_effect']} "
                                                    f"({a['state']})" for a in acts) + "\n"
        return md, "Jarvis Exports/action-list.md", "Export action list"
    m = re.search(r"(?:note|report)(?: (?:called|named|titled))?\s+[\"“]?([^\"”:]+?)[\"”]?\s*(?::|saying|with|that says)\s*(.+)$",
                  text, re.IGNORECASE | re.DOTALL)
    if m:
        title = m.group(1).strip()
        safe = re.sub(r"[^\w\- ]+", "", title).strip() or "note"
        return f"# {title}\n\n{m.group(2).strip()}\n", f"Notes/{safe}.md", f"Create note: {title}"
    return None, "Tell me the note title and content, e.g. “create a note called Rubric ideas: …”.", ""

