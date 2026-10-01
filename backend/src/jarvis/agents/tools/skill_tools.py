"""Skills as deferred tools (`skill__<name>`): calling one returns its instructions; helper files go to the sandbox."""
from __future__ import annotations

from jarvis.agents.tools import Tool, obj, tool
from jarvis.core.util import run_event
from jarvis.skills import service as SK


def _loader(name: str):
    def handler(run):
        s = SK.load(name)
        run_event(run.conn, run.run_id, "tool", f"Using skill: {name}")
        copied = []
        if s["files"]:
            from jarvis.agents.tools.sandbox import sandbox_available
            if sandbox_available(run.conn):
                from jarvis.sandbox import service as SB
                try:
                    for rel, text in s["files"].items():
                        SB.write_file(run.conn, run.run_id, f".skills/{name}/{rel}", text)
                        copied.append(f"/workspace/.skills/{name}/{rel}")
                except SB.SandboxError:
                    copied = []
        return {"skill": name, "instructions": s["body"],
                "helper_files": copied or sorted(s["files"]),
                "note": "These instructions were installed by AJ; follow them for this task. Helper files run only in "
                        "the sandbox."}
    return handler


def tools(conn) -> list[Tool]:
    return [Tool(f"skill__{s['name'].replace('-', '_')}", f"[Skill] {s['description']}", obj({}), _loader(s["name"]), "read",
                 "skills", manual=s["description"], deferred=True)
            for s in SK.list_skills(conn) if s["enabled"]]


@tool("propose_skill",
      "PROPOSE saving a reusable skill (a named routine with step-by-step instructions) when AJ asks you to remember HOW "
      "to do something. AJ reviews the exact text before it's installed.",
      obj({"name": {"type": "string", "description": "lowercase-with-dashes"}, "description": {"type": "string"},
           "instructions": {"type": "string"}}, ["name", "description", "instructions"]),
      risk="propose", family="skills", deferred=True, example="Teach yourself how I like my weekly report formatted.")
def propose_skill(run, name: str, description: str, instructions: str):
    import re

    from jarvis.actions.proposals import ActionSpec
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40]
    if not SK.NAME.match(name):
        return {"error": "Name must be 2–40 lowercase letters, digits or dashes."}
    run.specs.append(ActionSpec(action_type="skill.install", args={"name": name, "description": description[:500],
                                                                  "body": instructions[:20000]},
                                timezone=run.tz, target_resource="skills", target_name="Skills",
                                after={"name": name, "description": description[:500], "body": instructions[:20000]},
                                expected_effect=f"Install skill “{name}”: {description[:120]}", reason="Requested by AJ"))
    return {"proposed": True, "status": "awaiting AJ's review of the exact skill text"}
