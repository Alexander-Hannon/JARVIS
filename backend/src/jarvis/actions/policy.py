"""Tiered autonomy: AJ-authored rules that pre-approve low-risk (T1) actions, plus a short undo window.

Invariants (application code; the model has no path to any of this):
- Rules are created only through the authenticated Settings/Actions UI (`create_rule` is never exposed as a tool).
- A rule can only cover a T1 action type. T2/T3 types (deletes, anything reaching other people, money, locks,
  network egress) always need AJ's explicit approval, whatever rules exist.
- All-or-nothing: a proposal is auto-approved only when every item is covered by a live rule; otherwise it waits
  for AJ untouched (a partial auto-approval would silently skip the other items).
- A rule only applies to proposals from the origins AJ chose (default: his own direct commands), so work driven by
  untrusted content (a mission reading email, a forwarded channel message) can't ride on it.
- The auto-approval is an ordinary Approval row (channel='policy') bound to version + hash + items, so every executor
  guard still runs; revisions void it like any other approval.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from jarvis.actions import proposals as P
from jarvis.actions import registry as R
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow

UNDO_WINDOW = timedelta(minutes=5)
ORIGINS = ("command", "mission", "channel")


class PolicyError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------------------------------------ rules
def create_rule(conn: sqlite3.Connection, action_type: str, label: str, *, match: dict | None = None,
                origins: list[str] | None = None, daily_cap: int = 10, expires_at: str | None = None,
                session_id: str | None = None) -> dict:
    at = R.get(action_type)
    if at is None:
        raise PolicyError(400, "unknown_action", f"Unknown action type '{action_type}'.")
    if at.tier != R.Tier.T1:
        raise PolicyError(400, "not_t1", f"'{at.label}' always needs your explicit approval; it can't be automated.")
    origins = origins or ["command"]
    if any(o not in ORIGINS for o in origins):
        raise PolicyError(400, "bad_origin", f"Origins must be among {', '.join(ORIGINS)}.")
    if not 1 <= daily_cap <= 100:
        raise PolicyError(400, "bad_cap", "Daily cap must be between 1 and 100.")
    rid = new_id("rule")
    with tx(conn):
        conn.execute("INSERT INTO autonomy_rules (id, action_type, label, match, origins, daily_cap, created_at, "
                     "created_session, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rid, action_type, label.strip()[:120] or at.label, jdump(match or {}), jdump(origins), daily_cap,
                      now_iso(), session_id, expires_at))
        audit(conn, "user", "policy.rule_created", f"Auto-approve rule: {label} ({action_type})", "rule", rid,
              {"match": match or {}, "origins": origins, "daily_cap": daily_cap})
    return rule_view(conn, rid)


def revoke_rule(conn: sqlite3.Connection, rule_id: str) -> dict:
    with tx(conn):
        r = row(conn, "SELECT * FROM autonomy_rules WHERE id=?", (rule_id,))
        if not r:
            raise PolicyError(404, "not_found", "Rule not found.")
        conn.execute("UPDATE autonomy_rules SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now_iso(), rule_id))
        audit(conn, "user", "policy.rule_revoked", f"Rule revoked: {r['label']}", "rule", rule_id)
    return rule_view(conn, rule_id)


def _used_today(conn, rule_id: str) -> int:
    since = iso(utcnow() - timedelta(hours=24))
    r = row(conn, "SELECT COALESCE(SUM(action_count),0) n FROM policy_decisions WHERE rule_id=? AND decided_at>=?",
            (rule_id, since))
    return int(r["n"])


def rule_view(conn, rule_id: str) -> dict:
    r = row(conn, "SELECT * FROM autonomy_rules WHERE id=?", (rule_id,))
    at = R.get(r["action_type"])
    live = r["revoked_at"] is None and not (r["expires_at"] and parse_iso(r["expires_at"]) < utcnow())
    return {"id": r["id"], "action_type": r["action_type"], "action_label": at.label if at else r["action_type"],
            "label": r["label"], "match": jload(r["match"], {}), "origins": jload(r["origins"], []),
            "daily_cap": r["daily_cap"], "used_24h": _used_today(conn, r["id"]), "created_at": r["created_at"],
            "expires_at": r["expires_at"], "revoked_at": r["revoked_at"], "active": live}


def list_rules(conn) -> list[dict]:
    return [rule_view(conn, r["id"]) for r in rows(conn, "SELECT id FROM autonomy_rules ORDER BY created_at DESC")]


def catalog() -> list[dict]:
    """Every action type with its tier — feeds the Policies tab and the Help page."""
    return [{"action_type": a.name, "label": a.label, "provider": a.provider, "tier": int(a.tier),
             "automatable": a.tier == R.Tier.T1, "undo": a.undo, "reaches_others": a.reaches_others}
            for a in sorted(R.REGISTRY.values(), key=lambda a: a.name)]


# ------------------------------------------------------------------------------------------------ matching
def _matches(rule: dict, act: dict) -> bool:
    m = jload(rule["match"], {}) or {}
    if m.get("target_resource") and m["target_resource"] != act["target_resource"]:
        return False
    args = act["args"]
    return all(args.get(k) == v for k, v in (m.get("args") or {}).items())


def _live_rules(conn, action_type: str, origin: str) -> list[dict]:
    out = []
    for r in rows(conn, "SELECT * FROM autonomy_rules WHERE action_type=? AND revoked_at IS NULL ORDER BY created_at",
                  (action_type,)):
        if r["expires_at"] and parse_iso(r["expires_at"]) < utcnow():
            continue
        if origin not in jload(r["origins"], []):
            continue
        out.append(r)
    return out


def evaluate(conn, proposal_id: str) -> dict:
    """Decide (without writing) whether every item of the current version is covered by a live T1 rule."""
    p = P.current_version(conn, proposal_id)
    acts = P.actions_for_version(conn, p["pv"]["id"])
    origin = p.get("origin") or "command"
    if not acts or p["status"] != "proposed":
        return {"ok": False, "reason": "nothing to approve"}
    if P.control_paused(conn):
        return {"ok": False, "reason": "actions paused"}
    picks: dict[str, int] = {}
    cover: list[tuple[dict, dict]] = []
    for a in acts:
        at = R.get(a["action_type"])
        if at is None or at.tier != R.Tier.T1:
            return {"ok": False, "reason": f"{a['action_type']} always needs your approval"}
        if a["missing_fields"] or a["state"] != "proposed":
            return {"ok": False, "reason": "an item still needs details"}
        rule = next((r for r in _live_rules(conn, a["action_type"], origin)
                     if _matches(r, a) and _used_today(conn, r["id"]) + picks.get(r["id"], 0) < r["daily_cap"]), None)
        if rule is None:
            return {"ok": False, "reason": f"no rule covers {a['action_type']}"}
        picks[rule["id"]] = picks.get(rule["id"], 0) + 1
        cover.append((a, rule))
    return {"ok": True, "version": p["current_version"], "hash": p["pv"]["hash"], "items": [a["item_index"] for a, _ in cover],
            "rules": picks, "labels": sorted({r["label"] for _, r in cover})}


def try_auto_approve(conn: sqlite3.Connection, proposal_id: str) -> dict | None:
    """Called right after a proposal is created. Returns the decision when it auto-approved, else None."""
    if conn.in_transaction:
        return None   # never approve inside someone else's transaction; AJ approves manually instead
    d = evaluate(conn, proposal_id)
    if not d["ok"]:
        return None
    res = P.approve(conn, proposal_id, d["version"], d["hash"], d["items"], channel="policy", session_id=None,
                    evidence="Auto-approved by rule(s): " + ", ".join(d["labels"]))
    undo_until = iso(utcnow() + UNDO_WINDOW)
    with tx(conn):
        pv = P.current_version(conn, proposal_id)["pv"]
        for rid, n in d["rules"].items():
            conn.execute("INSERT INTO policy_decisions (id, rule_id, proposal_id, approval_id, action_count, decided_at) "
                         "VALUES (?,?,?,?,?,?)", (new_id("pd"), rid, proposal_id, res["approval_id"], n, now_iso()))
        for a in P.actions_for_version(conn, pv["id"]):
            if (at := R.get(a["action_type"])) and at.undo:
                conn.execute("UPDATE proposed_actions SET undo_until=? WHERE id=?", (undo_until, a["id"]))
        audit(conn, "policy", "approval.auto", "Auto-approved by rule(s): " + ", ".join(d["labels"]), "proposal",
              proposal_id, {"approval_id": res["approval_id"], "rules": d["rules"]})
    return d | {"approval_id": res["approval_id"], "undo_until": undo_until}


# ------------------------------------------------------------------------------------------------ undo
def _inverse(a: dict, inverse_type: str) -> P.ActionSpec:
    ext = a["external_id"]
    args = {"task.delete": {"task_id": ext}, "task.reopen": {"task_id": ext}, "task.complete": {"task_id": ext},
            "event.delete": {"event_id": ext}}.get(inverse_type)
    if inverse_type.endswith(".draft.delete"):
        args = {"draft_id": ext}
    if args is None or not ext:
        raise PolicyError(409, "not_undoable", "This action can't be undone automatically.")
    return P.ActionSpec(action_type=inverse_type, args=args, timezone=a["timezone"],
                        expected_effect=f"Undo: {a['expected_effect']}", account_id=a["account_id"],
                        target_resource=a["target_resource"], reason="Undo requested by AJ")


def undo(conn: sqlite3.Connection, action_id: str, session_id: str | None = None) -> dict:
    """AJ pressed Undo: create the inverse action and approve it in the same click (an explicit approval by AJ)."""
    a = row(conn, "SELECT * FROM proposed_actions WHERE id=?", (action_id,))
    if not a:
        raise PolicyError(404, "not_found", "Action not found.")
    at = R.get(a["action_type"])
    if not at or not at.undo:
        raise PolicyError(409, "not_undoable", "This action can't be undone automatically.")
    if a["undone_by"]:
        return {"proposal_id": a["undone_by"], "idempotent": True}
    if a["state"] != "verified":
        raise PolicyError(409, "not_verified", "Only a completed action can be undone.")
    if not a["undo_until"] or parse_iso(a["undo_until"]) < utcnow():
        raise PolicyError(410, "undo_expired", "The undo window has passed; ask me to reverse it instead.")
    a["args"] = jload(a["args"], {})
    spec = _inverse(a, at.undo)
    pv = row(conn, "SELECT proposal_id FROM proposal_versions WHERE id=?", (a["proposal_version_id"],))
    orig = row(conn, "SELECT conversation_id FROM proposals WHERE id=?", (pv["proposal_id"],))
    with tx(conn):
        pid = P.create(conn, f"Undo: {a['expected_effect']}", [spec], spec.expected_effect,
                       conversation_id=orig["conversation_id"])
        conn.execute("UPDATE proposed_actions SET undone_by=? WHERE id=?", (pid, action_id))
    view = P.view(conn, pid)
    P.approve(conn, pid, view["version"], view["hash"], [0], channel="button", session_id=session_id,
              evidence=f"Undo of {action_id}")
    return {"proposal_id": pid, "idempotent": False}
