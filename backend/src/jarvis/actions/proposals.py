"""Proposals, versions, approvals.

Invariants enforced here (application code, not prompts):
- Only allowlisted action types can be proposed or executed.
- An Approval binds to (proposal_version_id, proposal_hash, selected_items). Any revision creates a
  new version with a new hash; the older approval is revoked and its queued jobs cancelled.
- Approval requires: current version, matching hash, not expired, actions not paused, every selected
  item free of missing fields, and (for voice/typed) that this exact version was presented first.
- Approving the same version+selection twice is idempotent (no re-asking, no duplicate jobs).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from jarvis.core import jobs
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, canonical_hash, iso, new_id, now_iso, parse_iso, utcnow

ALLOWED_ACTIONS = {
    "task.create": "todoist", "task.update": "todoist", "task.complete": "todoist", "task.reopen": "todoist",
    "task.delete": "todoist",
    "event.create": "google_calendar", "event.update": "google_calendar", "event.delete": "google_calendar",
    "file.write": "local_fs", "file.move": "local_fs",
}
PROPOSAL_TTL = timedelta(hours=12)
EXECUTION_WINDOW = timedelta(hours=24)   # an approval older than this cannot start/ retry execution
TERMINAL_ITEM = {"verified", "failed", "unknown", "conflict", "cancelled", "skipped", "superseded"}


class ProposalError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class ActionSpec:
    action_type: str
    args: dict
    timezone: str
    expected_effect: str
    account_id: str | None = None
    target_resource: str | None = None
    target_name: str | None = None
    before: dict | None = None
    after: dict | None = None
    preconditions: dict = field(default_factory=dict)
    reason: str | None = None
    missing_fields: list[str] = field(default_factory=list)

    def canonical(self, index: int) -> dict:
        return {"i": index, "type": self.action_type, "account": self.account_id, "target": self.target_resource,
                "args": self.args, "tz": self.timezone, "pre": self.preconditions}


def _version_hash(specs: list[ActionSpec]) -> str:
    return canonical_hash([s.canonical(i) for i, s in enumerate(specs)])


def _insert_version(conn, proposal_id: str, version: int, specs: list[ActionSpec], summary: str, reason: str | None,
                    assumptions: list[str], questions: list[str], source: dict) -> str:
    for s in specs:
        if s.action_type not in ALLOWED_ACTIONS:
            raise ProposalError(400, "action_not_allowed", f"Action type '{s.action_type}' is not enabled in this release.")
    vid = new_id("pv")
    h = _version_hash(specs)
    now = utcnow()
    conn.execute(
        "INSERT INTO proposal_versions (id, proposal_id, version, hash, summary, reason, assumptions, questions, source, "
        "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (vid, proposal_id, version, h, summary, reason, jdump(assumptions), jdump(questions), jdump(source), iso(now),
         iso(now + PROPOSAL_TTL)))
    for i, s in enumerate(specs):
        conn.execute(
            "INSERT INTO proposed_actions (id, proposal_version_id, item_index, action_type, provider, account_id, "
            "target_resource, args, timezone, before_state, after_state, preconditions, expected_effect, reason, "
            "missing_fields, op_key, state, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'proposed', ?)",
            (new_id("act"), vid, i, s.action_type, ALLOWED_ACTIONS[s.action_type], s.account_id, s.target_resource,
             jdump(s.args | ({"_target_name": s.target_name} if s.target_name else {})), s.timezone,
             jdump(s.before) if s.before is not None else None, jdump(s.after) if s.after is not None else None,
             jdump(s.preconditions), s.expected_effect, s.reason, jdump(s.missing_fields),
             f"op_{canonical_hash([proposal_id, version, i])[:24]}", iso(now)))
    return vid


def create(conn: sqlite3.Connection, title: str, specs: list[ActionSpec], summary: str, *, conversation_id: str | None,
           run_id: str | None = None, capture_id: str | None = None, reason: str | None = None,
           assumptions: list[str] | None = None, questions: list[str] | None = None, source: dict | None = None) -> str:
    pid = new_id("prop")
    now = now_iso()
    conn.execute("INSERT INTO proposals (id, conversation_id, run_id, capture_id, status, current_version, title, created_at, "
                 "updated_at) VALUES (?,?,?,?, 'proposed', 1, ?,?,?)", (pid, conversation_id, run_id, capture_id, title, now, now))
    _insert_version(conn, pid, 1, specs, summary, reason, assumptions or [], questions or [], source or {})
    audit(conn, "model" if run_id else "user", "proposal.created", f"Proposed: {title}", "proposal", pid,
          {"items": [s.action_type for s in specs]})
    return pid


def current_version(conn, proposal_id: str) -> dict:
    p = row(conn, "SELECT * FROM proposals WHERE id=?", (proposal_id,))
    if not p:
        raise ProposalError(404, "not_found", "Proposal not found.")
    v = row(conn, "SELECT * FROM proposal_versions WHERE proposal_id=? AND version=?", (proposal_id, p["current_version"]))
    return p | {"pv": v}


def actions_for_version(conn, version_id: str) -> list[dict]:
    out = rows(conn, "SELECT * FROM proposed_actions WHERE proposal_version_id=? ORDER BY item_index", (version_id,))
    for a in out:
        a["args"] = jload(a["args"], {})
        a["preconditions"] = jload(a["preconditions"], {})
        a["missing_fields"] = jload(a["missing_fields"], [])
        a["before_state"] = jload(a["before_state"])
        a["after_state"] = jload(a["after_state"])
    return out


def specs_from_actions(actions: list[dict]) -> list[ActionSpec]:
    specs = []
    for a in actions:
        args = dict(a["args"])
        tname = args.pop("_target_name", None)
        specs.append(ActionSpec(action_type=a["action_type"], args=args, timezone=a["timezone"],
                                expected_effect=a["expected_effect"], account_id=a["account_id"],
                                target_resource=a["target_resource"], target_name=tname, before=a["before_state"],
                                after=a["after_state"], preconditions=a["preconditions"], reason=a["reason"],
                                missing_fields=list(a["missing_fields"])))
    return specs


def mark_presented(conn, proposal_id: str) -> None:
    p = current_version(conn, proposal_id)
    conn.execute("UPDATE proposal_versions SET presented_at=? WHERE id=? AND presented_at IS NULL", (now_iso(), p["pv"]["id"]))


def revise(conn: sqlite3.Connection, proposal_id: str, specs: list[ActionSpec], reason: str, *, summary: str | None = None,
           assumptions: list[str] | None = None, questions: list[str] | None = None, actor: str = "user") -> int:
    with tx(conn):
        p = current_version(conn, proposal_id)
        acts = actions_for_version(conn, p["pv"]["id"])
        started = [a for a in acts if a["state"] in ("executing", "verified", "unknown", "failed", "conflict")]
        if started or p["status"] not in ("proposed", "approved", "queued"):
            raise ProposalError(409, "not_revisable",
                                "This plan already started executing; changes need a new proposal (undo is a separate action).")
        _void_approvals_and_jobs(conn, p["pv"]["id"], "superseded by revision")
        new_v = p["current_version"] + 1
        conn.execute("UPDATE proposal_versions SET superseded_at=? WHERE id=?", (now_iso(), p["pv"]["id"]))
        conn.execute("UPDATE proposed_actions SET state='superseded', updated_at=? WHERE proposal_version_id=?",
                     (now_iso(), p["pv"]["id"]))
        _insert_version(conn, proposal_id, new_v, specs, summary or p["pv"]["summary"], reason,
                        assumptions if assumptions is not None else jload(p["pv"]["assumptions"], []),
                        questions if questions is not None else [], jload(p["pv"]["source"], {}))
        conn.execute("UPDATE proposals SET current_version=?, status='proposed', updated_at=?, row_version=row_version+1 "
                     "WHERE id=?", (new_v, now_iso(), proposal_id))
        audit(conn, actor, "proposal.revised", f"Revised to v{new_v}: {reason}", "proposal", proposal_id)
    return new_v


def _void_approvals_and_jobs(conn, version_id: str, why: str) -> None:
    appr = rows(conn, "SELECT id FROM approvals WHERE proposal_version_id=? AND revoked_at IS NULL", (version_id,))
    for a in appr:
        conn.execute("UPDATE approvals SET revoked_at=? WHERE id=?", (now_iso(), a["id"]))
    for act in rows(conn, "SELECT id FROM proposed_actions WHERE proposal_version_id=?", (version_id,)):
        conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE kind='execute_action' AND status IN ('queued','paused') "
                     "AND json_extract(payload,'$.action_id')=?", (now_iso(), act["id"]))
    if appr:
        audit(conn, "system", "approval.revoked", f"Approval revoked: {why}", "proposal_version", version_id)


def control_paused(conn) -> bool:
    r = row(conn, "SELECT actions_paused FROM control_state WHERE id=1")
    return bool(r and r["actions_paused"])


def approve(conn: sqlite3.Connection, proposal_id: str, version: int, hash_: str, selected_items: list[int], *,
            channel: str, session_id: str | None, evidence: str | None = None) -> dict:
    with tx(conn):
        p = current_version(conn, proposal_id)
        pv = p["pv"]
        if version != p["current_version"] or hash_ != pv["hash"]:
            raise ProposalError(409, "stale_version",
                                f"This approval refers to version {version}, but the current plan is version "
                                f"{p['current_version']}. Review the current plan.")
        if parse_iso(pv["expires_at"]) < utcnow():
            conn.execute("UPDATE proposals SET status='expired', updated_at=? WHERE id=?", (now_iso(), proposal_id))
            raise ProposalError(410, "expired", "This proposal expired; ask me to re-propose it.")
        if control_paused(conn):
            raise ProposalError(423, "paused", "Actions are paused. Resume actions before approving.")
        if channel in ("voice", "typed") and not pv["presented_at"]:
            raise ProposalError(409, "not_presented", "This plan wasn't presented yet; review it before approving.")
        acts = {a["item_index"]: a for a in actions_for_version(conn, pv["id"])}
        sel = sorted(set(selected_items))
        if not sel or any(i not in acts for i in sel):
            raise ProposalError(400, "bad_selection", "Select at least one listed item.")
        existing = row(conn, "SELECT * FROM approvals WHERE proposal_version_id=? AND revoked_at IS NULL", (pv["id"],))
        if existing:
            if jload(existing["selected_items"]) == sel:
                return {"approval_id": existing["id"], "idempotent": True}
            raise ProposalError(409, "already_approved", "A different selection of this version is already approved.")
        not_open = [i for i in sel if acts[i]["state"] != "proposed"]
        if not_open:
            raise ProposalError(409, "item_not_open",
                                f"Item(s) {', '.join(str(i + 1) for i in not_open)} are not awaiting approval.")
        missing = [i for i in sel if acts[i]["missing_fields"]]
        if missing:
            raise ProposalError(400, "missing_fields",
                                f"Item(s) {', '.join(str(i + 1) for i in missing)} still need details before approval.")
        if p["status"] not in ("proposed",):
            raise ProposalError(409, "bad_state", f"Proposal is {p['status']}.")
        aid = new_id("appr")
        conn.execute("INSERT INTO approvals (id, proposal_version_id, proposal_hash, selected_items, channel, session_id, "
                     "evidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (aid, pv["id"], pv["hash"], jdump(sel), channel, session_id, (evidence or "")[:500], now_iso()))
        for idx, a in acts.items():
            if idx in sel:
                conn.execute("UPDATE proposed_actions SET state='queued', updated_at=? WHERE id=?", (now_iso(), a["id"]))
                jobs.enqueue(conn, "execute_action", {"action_id": a["id"], "approval_id": aid},
                             dedupe_key=f"exec:{a['id']}:{aid}", max_attempts=4)
            else:
                conn.execute("UPDATE proposed_actions SET state='skipped', updated_at=? WHERE id=?", (now_iso(), a["id"]))
        conn.execute("UPDATE proposals SET status='queued', updated_at=?, row_version=row_version+1 WHERE id=?",
                     (now_iso(), proposal_id))
        audit(conn, "user", "approval.granted", f"Approved v{version} items {[i + 1 for i in sel]} via {channel}",
              "proposal", proposal_id, {"hash": pv["hash"], "approval_id": aid})
        return {"approval_id": aid, "idempotent": False}


def reject(conn: sqlite3.Connection, proposal_id: str, actor: str = "user") -> None:
    with tx(conn):
        p = current_version(conn, proposal_id)
        acts = actions_for_version(conn, p["pv"]["id"])
        if any(a["state"] in ("executing", "verified", "unknown") for a in acts):
            raise ProposalError(409, "already_executing",
                                "Some items already executed or are executing; I can't un-send them. Undo needs a new approved action.")
        _void_approvals_and_jobs(conn, p["pv"]["id"], "rejected")
        conn.execute("UPDATE proposed_actions SET state='cancelled', updated_at=? WHERE proposal_version_id=? AND state IN "
                     "('proposed','queued','approved','paused')", (now_iso(), p["pv"]["id"]))
        conn.execute("UPDATE proposals SET status='rejected', updated_at=? WHERE id=?", (now_iso(), proposal_id))
        audit(conn, actor, "proposal.rejected", f"Rejected: {p['title']}", "proposal", proposal_id)


def retry(conn: sqlite3.Connection, proposal_id: str, items: list[int]) -> list[str]:
    with tx(conn):
        if control_paused(conn):
            raise ProposalError(423, "paused", "Actions are paused.")
        p = current_version(conn, proposal_id)
        appr = row(conn, "SELECT * FROM approvals WHERE proposal_version_id=? AND revoked_at IS NULL", (p["pv"]["id"],))
        if not appr:
            raise ProposalError(409, "no_approval", "Nothing approved to retry.")
        if parse_iso(appr["created_at"]) + EXECUTION_WINDOW < utcnow():
            raise ProposalError(410, "approval_too_old", "That approval is over 24h old; ask me to re-propose.")
        acts = {a["item_index"]: a for a in actions_for_version(conn, p["pv"]["id"])}
        queued = []
        for i in items:
            a = acts.get(i)
            if not a or i not in jload(appr["selected_items"]):
                continue
            if a["state"] not in ("failed", "unknown"):
                continue  # verified/queued items are never re-run
            n = row(conn, "SELECT COUNT(*) c FROM execution_attempts WHERE action_id=?", (a["id"],))["c"]
            conn.execute("UPDATE proposed_actions SET state='queued', updated_at=? WHERE id=?", (now_iso(), a["id"]))
            jobs.enqueue(conn, "execute_action", {"action_id": a["id"], "approval_id": appr["id"], "retry": True},
                         dedupe_key=f"exec:{a['id']}:{appr['id']}:retry{n}", max_attempts=4)
            queued.append(a["id"])
        if queued:
            conn.execute("UPDATE proposals SET status='queued', updated_at=? WHERE id=?", (now_iso(), proposal_id))
            audit(conn, "user", "proposal.retry", f"Retry requested for {len(queued)} item(s)", "proposal", proposal_id)
        return queued


def expire_stale(conn: sqlite3.Connection) -> int:
    n = 0
    for p in rows(conn, "SELECT p.id FROM proposals p JOIN proposal_versions v ON v.proposal_id=p.id AND v.version=p.current_version "
                        "WHERE p.status='proposed' AND v.expires_at < ?", (now_iso(),)):
        conn.execute("UPDATE proposals SET status='expired', updated_at=? WHERE id=?", (now_iso(), p["id"]))
        n += 1
    return n


def recompute_status(conn: sqlite3.Connection, proposal_id: str) -> str:
    p = current_version(conn, proposal_id)
    states = [a["state"] for a in actions_for_version(conn, p["pv"]["id"]) if a["state"] not in ("skipped",)]
    if not states:
        status = p["status"]
    elif any(s in ("queued", "executing", "approved") for s in states):
        status = "executing" if "executing" in states else "queued"
    elif all(s == "verified" for s in states):
        status = "verified"
    elif any(s == "unknown" for s in states):
        status = "unknown"
    elif any(s == "verified" for s in states):
        status = "partial"
    elif all(s in ("cancelled", "paused") for s in states):
        status = "cancelled"
    else:
        status = "failed"
    conn.execute("UPDATE proposals SET status=?, updated_at=? WHERE id=?", (status, now_iso(), proposal_id))
    return status


# ---------- views ----------
def view(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any]:
    p = current_version(conn, proposal_id)
    pv = p["pv"]
    acts = actions_for_version(conn, pv["id"])
    appr = row(conn, "SELECT id, selected_items, channel, created_at FROM approvals WHERE proposal_version_id=? "
                     "AND revoked_at IS NULL", (pv["id"],))
    items = []
    for a in acts:
        rc = row(conn, "SELECT verified_at, method, ok, simulated, observed FROM verification_receipts WHERE action_id=? "
                       "ORDER BY verified_at DESC LIMIT 1", (a["id"],))
        if rc:
            rc["observed"] = jload(rc["observed"], {})
            rc["ok"], rc["simulated"] = bool(rc["ok"]), bool(rc["simulated"])
        acct = row(conn, "SELECT label FROM integration_accounts WHERE id=?", (a["account_id"],)) if a["account_id"] else None
        args = dict(a["args"])
        tname = args.pop("_target_name", None)
        items.append({"index": a["item_index"], "action_id": a["id"], "action_type": a["action_type"], "provider": a["provider"],
                      "account_label": acct["label"] if acct else None,
                      "target": {"id": a["target_resource"], "name": tname}, "args": args, "timezone": a["timezone"],
                      "before": a["before_state"], "after": a["after_state"], "expected_effect": a["expected_effect"],
                      "reason": a["reason"], "missing_fields": a["missing_fields"], "state": a["state"],
                      "external_id": a["external_id"], "external_url": a["external_url"], "last_error": a["last_error"],
                      "receipt": rc})
    versions = rows(conn, "SELECT version, hash, created_at, superseded_at FROM proposal_versions WHERE proposal_id=? "
                          "ORDER BY version", (proposal_id,))
    if appr:
        appr["selected_items"] = jload(appr["selected_items"])
    return {"id": p["id"], "status": p["status"], "title": p["title"], "version": p["current_version"], "hash": pv["hash"],
            "summary": pv["summary"], "assumptions": jload(pv["assumptions"], []), "questions": jload(pv["questions"], []),
            "expires_at": pv["expires_at"], "presented_at": pv["presented_at"], "conversation_id": p["conversation_id"],
            "created_at": p["created_at"], "items": items, "versions": versions, "approval": appr}


def active_for_conversation(conn, conversation_id: str) -> dict | None:
    return row(conn, "SELECT id FROM proposals WHERE conversation_id=? AND status='proposed' ORDER BY created_at DESC LIMIT 1",
               (conversation_id,))


def latest_for_conversation(conn, conversation_id: str) -> dict | None:
    return row(conn, "SELECT id, status FROM proposals WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
               (conversation_id,))
