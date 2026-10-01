"""Executor handler for sandbox network windows (approved by AJ; re-validated here)."""
from __future__ import annotations

from jarvis.actions.executor import Blocked
from jarvis.sandbox import service as S


def h_network(conn, a: dict, reconcile_first: bool) -> dict:
    try:
        n = S.open_network(conn, a["args"]["domains"], a["args"]["minutes"])
    except S.SandboxError as e:
        raise Blocked("failed", e.message) from None
    # Setting the same window twice is harmless: naturally idempotent.
    return {"ok": n["active"], "method": "state_set", "simulated": S.get_runtime().simulated,
            "observed": n, "message": f"Sandbox can reach {', '.join(n['domains'])} until {n['until']}"}


def h_skill_install(conn, a: dict, reconcile_first: bool) -> dict:
    from jarvis.skills import service as SK
    args = a["args"]
    try:
        s = SK.write_skill(conn, args["name"], args["description"], args["body"])
    except SK.SkillError as e:
        raise Blocked("failed", e.message) from None
    return {"ok": True, "method": "read_after_write", "simulated": False, "observed": {"skill": s["name"]},
            "message": f"Skill installed: {s['name']}"}
