"""Sandbox tools: a disposable Linux workspace (shell, Python, files, a browser) inside Docker — never the host.

Running code in the sandbox needs no approval (it can't touch AJ's machine or accounts). What does need AJ:
network egress (an expiring domain window, `sandbox.network`, T2) and getting files out (a `file.write` proposal into
his workspace folder). Desktop control uses the computer toolset (handled in the agent loop) on the sandbox display.
"""
from __future__ import annotations

import re

from jarvis.agents.tools import obj, tool
from jarvis.core.util import run_event
from jarvis.sandbox import service as S
from jarvis.sandbox.runtime import get_runtime


def sandbox_available(conn) -> bool:
    try:
        return bool(get_runtime().available()[0])
    except Exception:  # noqa: BLE001
        return False


def _err(e: S.SandboxError) -> dict:
    return {"error": e.message, "code": e.code}


@tool("sandbox_run",
      "Run a bash command in JARVIS's disposable Linux sandbox (Python 3.12, pandas, numpy, matplotlib, git, jq, curl, "
      "Chromium via Playwright). Working dir /workspace persists until reset. No access to AJ's computer or accounts; "
      "internet only for domains AJ approved in a network window. Use for calculations, data analysis, scripts, file "
      "conversion and building things.",
      obj({"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300}}, ["command"]),
      risk="act", family="sandbox", deferred=True, available=sandbox_available,
      manual="Runs code in an isolated sandbox container. It can't see your files or accounts; internet only for "
             "domains you approve.", example="Analyze this CSV and chart spending by month.")
def sandbox_run(run, command: str, timeout_s: int = 120):
    run_event(run.conn, run.run_id, "tool", f"Sandbox: {command[:80]}")
    try:
        return S.run(run.conn, run.run_id, command, timeout_s)
    except S.SandboxError as e:
        return _err(e)


@tool("sandbox_write_file", "Write a text file inside the sandbox (/workspace/...).",
      obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
      risk="act", family="sandbox", deferred=True, available=sandbox_available)
def sandbox_write_file(run, path: str, content: str):
    try:
        return S.write_file(run.conn, run.run_id, path, content)
    except S.SandboxError as e:
        return _err(e)


@tool("sandbox_read_file", "Read a text file from the sandbox (/workspace/...).", obj({"path": {"type": "string"}}, ["path"]),
      risk="read", family="sandbox", deferred=True, available=sandbox_available)
def sandbox_read_file(run, path: str):
    try:
        return {"text": S.read_file(run.conn, run.run_id, path, 20000), "note": "Untrusted data."}
    except S.SandboxError as e:
        return _err(e)


@tool("browser_open",
      "Open a web page in the sandbox's real Chromium (JavaScript runs) and return its title and visible text; a "
      "screenshot is shown to AJ. Only works for domains inside an approved network window. For simple reading, prefer "
      "web_fetch.",
      obj({"url": {"type": "string"}}, ["url"]), risk="act", family="sandbox", deferred=True, available=sandbox_available,
      example="Check the parking permit page on the UAH site.")
def browser_open(run, url: str):
    run_event(run.conn, run.run_id, "tool", f"Opening {url} in the sandbox browser")
    try:
        r = S.browse(run.conn, run.run_id, url)
    except S.SandboxError as e:
        return _err(e)
    if r.get("screenshot_id"):
        run.visuals["sandbox_shot"] = {"type": "screenshot", "shot_id": r["screenshot_id"], "title": r.get("title"),
                                       "url": r.get("url")}
    return {k: r[k] for k in ("title", "url", "text", "note")}


@tool("propose_sandbox_network",
      "PROPOSE opening the sandbox's internet access to specific domains for a limited time (needs AJ's explicit "
      "approval). List the exact domains needed, e.g. pypi.org, files.pythonhosted.org.",
      obj({"domains": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
           "minutes": {"type": "integer", "minimum": 5, "maximum": 240}, "why": {"type": "string"}}, ["domains", "why"]),
      risk="propose", family="sandbox", deferred=True, available=sandbox_available)
def propose_sandbox_network(run, domains: list[str], why: str, minutes: int = 30):
    from jarvis.actions.proposals import ActionSpec
    try:
        clean = S.validate_domains(domains)
    except S.SandboxError as e:
        return _err(e)
    run.specs.append(ActionSpec(action_type="sandbox.network", args={"domains": clean, "minutes": int(minutes)},
                                timezone=run.tz, target_resource="sandbox", target_name="Sandbox",
                                after={"domains": clean, "minutes": int(minutes), "why": why[:300]},
                                expected_effect=f"Let the sandbox reach {', '.join(clean)} for {int(minutes)} min",
                                reason=why[:300]))
    return {"proposed": True, "status": "awaiting AJ's explicit approval; retry the network step after he approves"}


@tool("propose_export_file",
      "PROPOSE copying a text file out of the sandbox into AJ's writable workspace folder (diff preview + approval).",
      obj({"sandbox_path": {"type": "string"}, "dest_path": {"type": "string", "description": "path inside the workspace folder"}},
          ["sandbox_path", "dest_path"]),
      risk="propose", family="sandbox", deferred=True, available=sandbox_available)
def propose_export_file(run, sandbox_path: str, dest_path: str):
    from jarvis.actions.proposals import ActionSpec
    from jarvis.localops import files as F
    roots = [r for r in F.list_roots(run.conn) if r["writable"]]
    if not roots:
        return {"error": "No writable workspace folder designated (Settings → Local files)."}
    try:
        content = S.read_file(run.conn, run.run_id, sandbox_path, 1_000_000)
        plan = F.plan_write(run.conn, roots[0]["id"], re.sub(r"^/+", "", dest_path), content)
    except S.SandboxError as e:
        return _err(e)
    except F.FileOpError as e:
        return {"error": e.message}
    run.specs.append(ActionSpec(action_type="file.write", args=plan["args"], timezone=run.tz, target_resource=roots[0]["id"],
                                target_name=roots[0]["label"], before=plan["before"],
                                after=plan["after"] | {"diff": plan["diff"]},
                                expected_effect=f"Save sandbox file {sandbox_path} to {roots[0]['label']}/{dest_path}",
                                reason="Requested by AJ"))
    return {"proposed": True, "status": "awaiting AJ's approval"}
