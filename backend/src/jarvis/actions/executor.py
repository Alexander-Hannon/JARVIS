"""Approved-action executor (runs inside the worker via the `execute_action` job).

Sequence per action:
  1. Guard (DB): approval valid for this exact version/hash/item, version current, approval fresh,
     actions not paused, action type allowlisted, account + resource write grant still valid.
  2. Mark `executing` + insert ExecutionAttempt and COMMIT before touching the provider. A crash after
     this point leaves `executing`, which the next run treats as an unknown outcome.
  3. If a previous attempt's outcome is unknown: reconcile first (marker search / deterministic event id /
     state comparison). Found => verified without re-sending.
  4. Refresh preconditions (calendar free/busy, remote task snapshot, etag).
  5. Provider write with a stable idempotency key; then read-after-write verification => receipt.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime

from jarvis.actions import proposals as P
from jarvis.actions import registry as R
from jarvis.core.db import jdump, jload, row, tx
from jarvis.core.util import audit, canonical_hash, new_id, now_iso, parse_iso, record_latency, run_event, utcnow
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError

log = logging.getLogger("jarvis.executor")
OP_NAMESPACE = uuid.UUID("7d8a3c52-4b9e-4d1a-9a55-3f0c2b7e6a10")


class Blocked(Exception):
    """Execution must not proceed; not retryable without a new approval/proposal."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state, self.message = state, message


def op_uuid(op_key: str) -> str:
    return str(uuid.uuid5(OP_NAMESPACE, op_key))


def marker(op_key: str) -> str:
    return f"[jarvis:op={op_key}]"


def _load(conn, action_id: str) -> tuple[dict, dict, dict]:
    a = row(conn, "SELECT * FROM proposed_actions WHERE id=?", (action_id,))
    if not a:
        raise Blocked("failed", "Action not found.")
    pv = row(conn, "SELECT * FROM proposal_versions WHERE id=?", (a["proposal_version_id"],))
    p = row(conn, "SELECT * FROM proposals WHERE id=?", (pv["proposal_id"],))
    a["args"] = jload(a["args"], {})
    a["args"].pop("_target_name", None)
    a["preconditions"] = jload(a["preconditions"], {})
    a["before_state"] = jload(a["before_state"])
    return a, pv, p


def _guard(conn, a: dict, pv: dict, p: dict, approval_id: str) -> None:
    appr = row(conn, "SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not appr or appr["revoked_at"]:
        raise Blocked("cancelled", "Approval was revoked (plan changed or was rejected).")
    if appr["proposal_version_id"] != pv["id"] or appr["proposal_hash"] != pv["hash"]:
        raise Blocked("cancelled", "Approval does not match this plan version.")
    if a["item_index"] not in jload(appr["selected_items"], []):
        raise Blocked("skipped", "Item was not selected for approval.")
    if p["current_version"] != pv["version"] or pv["superseded_at"]:
        raise Blocked("superseded", "Plan was revised after approval; the old version will not run.")
    if parse_iso(appr["created_at"]) + P.EXECUTION_WINDOW < utcnow():
        raise Blocked("cancelled", "Approval is older than the execution window; re-propose to run it.")
    at = R.get(a["action_type"])
    if at is None or R.provider_for(at.name) != a["provider"]:
        raise Blocked("failed", f"Action type {a['action_type']} is not enabled.")
    if at.account_provider is None:
        return  # local operator checks its own root grants
    acct = A.get_account(conn, at.account_provider)
    override = a["provider"] in A._overrides   # labeled contract fake installed (tests / simulated mode)
    if (acct is None and not override) or (acct is not None and acct["id"] != a["account_id"]):
        raise Blocked("failed", "The target account changed or was disconnected since approval; approval is void.")
    if acct is not None and acct["status"] not in ("connected", "error"):
        raise Blocked("failed", f"{at.account_provider} is {acct['status']}; reconnect it.")
    if a["account_id"] and at.grant_type and (at.grant_always or a["target_resource"]):
        if not A.has_grant(conn, a["account_id"], at.grant_type, a["target_resource"] or "", "write"):
            raise Blocked("failed", f"No write permission recorded for {at.grant_type} '{a['target_resource']}'.")


def _set_state(conn, a_id: str, state: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE proposed_actions SET state=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
                 (state, now_iso(), *fields.values(), a_id))


def _receipt(conn, a_id: str, method: str, ok: bool, provider_ids: dict, observed: dict, simulated: bool) -> None:
    conn.execute("INSERT INTO verification_receipts (id, action_id, verified_at, method, ok, provider_ids, observed, simulated) "
                 "VALUES (?,?,?,?,?,?,?,?)", (new_id("rcpt"), a_id, now_iso(), method, int(ok), jdump(provider_ids),
                                             jdump(observed), int(simulated)))


def _finish_attempt(conn, attempt_id: str, outcome: str, summary: dict | None = None, error: str | None = None) -> None:
    conn.execute("UPDATE execution_attempts SET finished_at=?, outcome=?, response_summary=?, error=? WHERE id=?",
                 (now_iso(), outcome, jdump(summary or {}), error, attempt_id))


def execute(conn: sqlite3.Connection, action_id: str, approval_id: str, job_id: str | None = None) -> dict:
    t0 = utcnow()
    # ---- phase 1: guard + claim (single transaction) ----
    with tx(conn):
        a, pv, p = _load(conn, action_id)
        prior_unknown = a["state"] in ("executing", "unknown")
        if a["state"] in ("verified", "cancelled", "skipped", "superseded"):
            return {"state": a["state"], "noop": True}
        if P.control_paused(conn):
            _set_state(conn, a["id"], "paused")
            audit(conn, "worker", "action.paused", "Action held: actions are paused", "action", a["id"])
            P.recompute_status(conn, p["id"])
            return {"state": "paused"}
        try:
            _guard(conn, a, pv, p, approval_id)
        except Blocked as b:
            _set_state(conn, a["id"], b.state, last_error=b.message)
            audit(conn, "worker", "action.blocked", b.message, "action", a["id"])
            run_event(conn, p["run_id"], "error", f"Not executed: {b.message}")
            P.recompute_status(conn, p["id"])
            return {"state": b.state, "error": b.message}
        n = row(conn, "SELECT COUNT(*) c FROM execution_attempts WHERE action_id=?", (a["id"],))["c"]
        if a["state"] == "executing":
            conn.execute("UPDATE execution_attempts SET outcome='unknown', finished_at=?, error='worker stopped mid-write' "
                         "WHERE action_id=? AND finished_at IS NULL", (now_iso(), a["id"]))
        attempt_id = new_id("att")
        conn.execute("INSERT INTO execution_attempts (id, action_id, job_id, attempt_no, started_at, request_hash) "
                     "VALUES (?,?,?,?,?,?)", (attempt_id, a["id"], job_id, n + 1, now_iso(),
                                              canonical_hash([a["action_type"], a["args"]])))
        _set_state(conn, a["id"], "executing")
        run_event(conn, p["run_id"], "status", f"Executing: {a['expected_effect']}")

    # ---- phase 2: provider I/O (no DB transaction held) ----
    try:
        handler = R.get(a["action_type"]).resolve_handler()
        result = handler(conn, a, reconcile_first=prior_unknown)
    except Blocked as b:
        with tx(conn):
            _finish_attempt(conn, attempt_id, "precondition_failed" if b.state == "conflict" else "blocked", error=b.message)
            _set_state(conn, a["id"], b.state, last_error=b.message)
            run_event(conn, p["run_id"], "error", f"Stopped before writing: {b.message}")
            audit(conn, "worker", "action.precondition_failed", b.message, "action", a["id"])
            P.recompute_status(conn, p["id"])
        return {"state": b.state, "error": b.message}
    except A.NotConnected as e:
        with tx(conn):
            _finish_attempt(conn, attempt_id, "failed", error=str(e))
            _set_state(conn, a["id"], "failed", last_error=str(e))
            P.recompute_status(conn, p["id"])
        return {"state": "failed", "error": str(e)}
    except ProviderError as e:
        with tx(conn):
            if not e.outcome_known:
                _finish_attempt(conn, attempt_id, "unknown", error=f"{e.kind}: {e.message}")
                _set_state(conn, a["id"], "unknown", last_error=f"Outcome unknown ({e.kind}); will reconcile before any retry.")
                run_event(conn, p["run_id"], "error", "Provider response lost; outcome unknown — reconciling before retry.")
                state = "unknown"
            else:
                _finish_attempt(conn, attempt_id, "failed", error=f"{e.kind}: {e.message}")
                retry_state = "queued" if e.retryable else "failed"
                _set_state(conn, a["id"], retry_state, last_error=f"{e.kind}: {e.message}")
                if e.kind == "auth":
                    acct_id = a["account_id"]
                    if acct_id:
                        A.set_status(conn, acct_id, "error", "Authorization expired or revoked; reconnect required.")
                state = retry_state
            audit(conn, "worker", "action.error", f"{a['action_type']} {e.kind}: {e.message}", "action", a["id"])
            P.recompute_status(conn, p["id"])
        return {"state": state, "error": e.message, "retryable": (not e.outcome_known) or e.retryable,
                "retry_after": e.retry_after}

    # ---- phase 3: record verified outcome ----
    with tx(conn):
        _finish_attempt(conn, attempt_id, "success", result.get("summary"))
        _receipt(conn, a["id"], result["method"], result["ok"], result.get("provider_ids", {}), result.get("observed", {}),
                 result.get("simulated", False))
        state = "verified" if result["ok"] else "failed"
        _set_state(conn, a["id"], state, external_id=result.get("external_id"), external_url=result.get("url"),
                   etag=result.get("etag"), last_error=None if result["ok"] else "Read-back did not match the approved values.")
        if result.get("link"):
            result["link"](conn, a)
        msg = result.get("message") or a["expected_effect"]
        run_event(conn, p["run_id"], "verify", ("Verified: " if result["ok"] else "Verification mismatch: ") + msg,
                  {"simulated": result.get("simulated", False)})
        audit(conn, "worker", "action.verified" if result["ok"] else "action.verify_mismatch", msg, "action", a["id"],
              {"method": result["method"], "simulated": result.get("simulated", False)})
        status = P.recompute_status(conn, p["id"])
        if status in ("verified", "partial", "failed", "unknown"):
            from jarvis.briefings.notify import notify
            notify(conn, "action_result", f"{p['title']}: {status}", _status_line(conn, pv["id"]), "proposal", p["id"])
        record_latency(conn, "execution", (utcnow() - t0).total_seconds() * 1000, {"action_type": a["action_type"]})
    return {"state": state}


def _status_line(conn, version_id: str) -> str:
    acts = P.actions_for_version(conn, version_id)
    return "; ".join(f"{i['item_index'] + 1}. {i['state']}" for i in acts if i["state"] != "skipped")


# ================= handlers =================
def _task_fields(args: dict) -> dict:
    keys = ("content", "description", "project_id", "due_date", "due_datetime", "due_string", "due_lang", "duration",
            "duration_unit", "priority", "labels")
    return {k: args[k] for k in keys if args.get(k) not in (None, "", [])}


def _check_task(t, args: dict) -> tuple[bool, dict]:
    observed = {"content": t.content, "due": t.due, "project_id": t.project_id, "completed": t.is_completed}
    ok = t.content.strip() == args.get("content", t.content).strip()
    if args.get("due_date") and t.due:
        ok = ok and (t.due.get("date", "")[:10] == args["due_date"])
    if args.get("due_datetime") and t.due:
        ok = ok and bool(t.due.get("datetime") or t.due.get("date"))
    if args.get("project_id"):
        ok = ok and str(t.project_id) == str(args["project_id"])
    return ok, observed


def _link_task(status: str):
    def link(conn, a):
        args = a["args"]
        conn.execute(
            "INSERT INTO task_links (id, op_key, action_id, account_id, external_id, url, content, project_ext_id, due, status, "
            "last_synced_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(op_key) DO UPDATE SET external_id=excluded.external_id, url=excluded.url, status=excluded.status, "
            "last_synced_at=excluded.last_synced_at, updated_at=excluded.updated_at",
            (new_id("tl"), a["op_key"], a["id"], a["account_id"], a.get("_ext_id"), a.get("_url"), args.get("content", ""),
             args.get("project_id"), jdump({k: args.get(k) for k in ("due_date", "due_datetime", "due_string")}), status,
             now_iso(), now_iso(), now_iso()))
        if a["action_type"] != "task.create" and args.get("task_id"):
            conn.execute("UPDATE task_links SET status=?, updated_at=? WHERE external_id=?", (status, now_iso(), args["task_id"]))
    return link


def h_task_create(conn, a: dict, reconcile_first: bool) -> dict:
    tp = A.task_provider(conn)
    args = dict(a["args"])
    mk = marker(a["op_key"])
    sim = A.is_simulated(tp)
    if reconcile_first:
        found = tp.find_by_marker(mk, args.get("project_id"))
        if found:
            ok, obs = _check_task(found, args)
            a["_ext_id"], a["_url"] = found.id, found.url
            return {"ok": ok, "method": "reconcile_marker", "external_id": found.id, "url": found.url, "simulated": sim,
                    "provider_ids": {"task_id": found.id}, "observed": obs, "link": _link_task("active"),
                    "message": f"Found existing Todoist task (no duplicate created): {found.content}"}
    fields = _task_fields(args)
    fields["description"] = (fields.get("description", "") + "\n\n" + mk).strip()
    created = tp.create_task(op_uuid(a["op_key"]), fields)
    fetched = tp.get_task(created.id)
    if fetched is None:
        raise ProviderError("server", "Created task could not be read back.", outcome_known=False)
    ok, obs = _check_task(fetched, args)
    a["_ext_id"], a["_url"] = fetched.id, fetched.url
    return {"ok": ok, "method": "read_after_write", "external_id": fetched.id, "url": fetched.url, "simulated": sim,
            "provider_ids": {"task_id": fetched.id}, "observed": obs, "link": _link_task("active"),
            "summary": {"task_id": fetched.id}, "message": f"Task verified in Todoist: {fetched.content}"}


def _remote_snapshot_check(t, before: dict | None) -> None:
    if not before:
        return
    if t is None:
        raise Blocked("conflict", "The task no longer exists in Todoist.")
    if before.get("content") and t.content != before["content"]:
        raise Blocked("conflict", "The task was edited in Todoist after this plan was made; re-propose to avoid overwriting it.")


def h_task_update(conn, a: dict, reconcile_first: bool) -> dict:
    tp = A.task_provider(conn)
    args = dict(a["args"])
    tid = args.pop("task_id")
    current = tp.get_task(tid)
    desired = _task_fields(args)
    if current and reconcile_first:
        ok, obs = _check_task(current, desired | {"content": desired.get("content", current.content)})
        if ok:
            return {"ok": True, "method": "reconcile_id", "external_id": tid, "url": current.url, "observed": obs,
                    "simulated": A.is_simulated(tp), "provider_ids": {"task_id": tid}}
    _remote_snapshot_check(current, a["before_state"])
    tp.update_task(op_uuid(a["op_key"]), tid, desired)
    after = tp.get_task(tid)
    ok, obs = _check_task(after, desired | {"content": desired.get("content", after.content)}) if after else (False, {})
    return {"ok": ok, "method": "read_after_write", "external_id": tid, "url": after.url if after else None, "observed": obs,
            "simulated": A.is_simulated(tp), "provider_ids": {"task_id": tid}, "message": f"Task updated: {obs.get('content')}"}


def h_task_complete(conn, a: dict, reconcile_first: bool) -> dict:
    tp = A.task_provider(conn)
    tid = a["args"]["task_id"]
    current = tp.get_task(tid)
    if current is None:
        raise Blocked("conflict", "Task not found in Todoist (deleted?).")
    if not current.is_completed:
        _remote_snapshot_check(current, a["before_state"])
        # Completing a parent never implies completing its subtasks in Jarvis; Todoist's own behavior is surfaced in the plan.
        tp.close_task(op_uuid(a["op_key"]), tid)
    after = tp.get_task(tid)
    ok = after is not None and after.is_completed
    return {"ok": ok, "method": "read_after_write", "external_id": tid, "url": current.url, "simulated": A.is_simulated(tp),
            "observed": {"completed": bool(after and after.is_completed)}, "provider_ids": {"task_id": tid},
            "link": _link_task("completed"), "message": f"Task completed in Todoist: {current.content}"}


def h_task_reopen(conn, a: dict, reconcile_first: bool) -> dict:
    tp = A.task_provider(conn)
    tid = a["args"]["task_id"]
    current = tp.get_task(tid)
    if current is None:
        raise Blocked("conflict", "Task not found in Todoist.")
    if current.is_completed:
        tp.reopen_task(op_uuid(a["op_key"]), tid)
    after = tp.get_task(tid)
    ok = after is not None and not after.is_completed
    return {"ok": ok, "method": "read_after_write", "external_id": tid, "simulated": A.is_simulated(tp),
            "observed": {"completed": bool(after and after.is_completed)}, "link": _link_task("active")}


def h_task_delete(conn, a: dict, reconcile_first: bool) -> dict:
    tp = A.task_provider(conn)
    tid = a["args"]["task_id"]
    current = tp.get_task(tid)
    if current is not None and not current.is_deleted:
        _remote_snapshot_check(current, a["before_state"])
        tp.delete_task(op_uuid(a["op_key"]), tid)
    after = tp.get_task(tid)
    ok = after is None or after.is_deleted
    return {"ok": ok, "method": "read_after_write", "external_id": tid, "simulated": A.is_simulated(tp),
            "observed": {"exists": not ok}, "link": _link_task("deleted"), "message": "Task deleted in Todoist"}


def _event_link(conn, a):
    args = a["args"]
    conn.execute(
        "INSERT INTO calendar_links (id, op_key, action_id, account_id, calendar_id, event_id, etag, html_link, summary, start_at, "
        "end_at, tz, task_link_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(op_key) DO UPDATE SET etag=excluded.etag, status=excluded.status, updated_at=excluded.updated_at",
        (new_id("cl"), a["op_key"], a["id"], a["account_id"], a["target_resource"], a.get("_ext_id"), a.get("_etag"),
         a.get("_url"), args.get("summary", ""), args.get("start", {}).get("dateTime"), args.get("end", {}).get("dateTime"),
         args.get("start", {}).get("timeZone"), args.get("task_link_id"), a.get("_link_status", "active"), now_iso(), now_iso()))


def _check_event(ev, args: dict) -> tuple[bool, dict]:
    obs = {"summary": ev.summary, "start": ev.start, "end": ev.end, "status": ev.status}

    def same(x: dict, y: dict) -> bool:
        if "dateTime" in y:
            return "dateTime" in x and parse_iso(x["dateTime"]) == parse_iso(y["dateTime"])
        return x.get("date") == y.get("date")
    ok = ev.summary == args.get("summary", ev.summary) and ev.status != "cancelled"
    if args.get("start"):
        ok = ok and same(ev.start, args["start"])
    if args.get("end"):
        ok = ok and same(ev.end, args["end"])
    return ok, obs


def _conflict_check(cp, a: dict, start: str, end: str, ignore_event_id: str | None = None) -> None:
    cals = list(dict.fromkeys((a["preconditions"].get("check_calendars") or []) + [a["target_resource"]]))
    busy = cp.free_busy(cals, start, end)
    s, e = parse_iso(start), parse_iso(end)
    own = []
    if ignore_event_id:
        ev = cp.get_event(a["target_resource"], ignore_event_id)
        if ev and "dateTime" in ev.start:
            own.append((parse_iso(ev.start["dateTime"]), parse_iso(ev.end["dateTime"])))
    for cal, intervals in busy.items():
        for iv in intervals:
            bs, be = parse_iso(iv["start"]), parse_iso(iv["end"])
            if bs < e and be > s and not any(bs == o[0] and be == o[1] for o in own):
                raise Blocked("conflict", f"New conflict on calendar {cal} ({_fmt(bs)}–{_fmt(be)}) since this plan was "
                                          "approved; nothing was written. Ask me for a new slot.")


def _fmt(dt: datetime) -> str:
    return dt.strftime("%H:%M UTC")


def h_event_create(conn, a: dict, reconcile_first: bool) -> dict:
    from jarvis.integrations.google_calendar import event_id_from_op_key
    cp = A.calendar_provider(conn)
    args = dict(a["args"])
    if args.get("attendees"):
        raise Blocked("failed", "Invitations/attendees are not supported in this release.")
    cal = a["target_resource"]
    eid = event_id_from_op_key(a["op_key"])
    sim = A.is_simulated(cp)
    existing = cp.get_event(cal, eid)
    if existing and existing.status != "cancelled":
        ok, obs = _check_event(existing, args)
        a.update(_ext_id=eid, _etag=existing.etag, _url=existing.html_link)
        return {"ok": ok, "method": "reconcile_id", "external_id": eid, "url": existing.html_link, "etag": existing.etag,
                "observed": obs, "simulated": sim, "provider_ids": {"calendar_id": cal, "event_id": eid}, "link": _event_link,
                "message": f"Event already on calendar (no duplicate): {existing.summary}"}
    if "dateTime" in args["start"]:
        _conflict_check(cp, a, args["start"]["dateTime"], args["end"]["dateTime"])
    body = {k: v for k, v in args.items() if k in ("summary", "description", "start", "end", "location", "transparency",
                                                    "reminders", "colorId")}
    body["description"] = (body.get("description", "") + "\n\n" + marker(a["op_key"])).strip()
    body["extendedProperties"] = {"private": {"jarvis_op": a["op_key"]}}
    cp.insert_event(cal, eid, body)
    ev = cp.get_event(cal, eid)
    if ev is None:
        raise ProviderError("server", "Created event could not be read back.", outcome_known=False)
    ok, obs = _check_event(ev, args)
    a.update(_ext_id=eid, _etag=ev.etag, _url=ev.html_link)
    return {"ok": ok, "method": "read_after_write", "external_id": eid, "url": ev.html_link, "etag": ev.etag, "observed": obs,
            "simulated": sim, "provider_ids": {"calendar_id": cal, "event_id": eid}, "link": _event_link,
            "message": f"Event verified on Google Calendar: {ev.summary}"}


def h_event_update(conn, a: dict, reconcile_first: bool) -> dict:
    cp = A.calendar_provider(conn)
    args = dict(a["args"])
    eid, cal = args.pop("event_id"), a["target_resource"]
    scope = args.pop("recurrence_scope", None)
    cur = cp.get_event(cal, eid)
    if cur is None:
        raise Blocked("conflict", "Event no longer exists.")
    if cur.recurring_event_id is None and cur.raw.get("recurrence") and scope != "series":
        raise Blocked("failed", "Recurring series edit needs explicit 'series' scope.")
    if reconcile_first:
        ok, obs = _check_event(cur, args)
        if ok:
            return {"ok": True, "method": "reconcile_id", "external_id": eid, "etag": cur.etag, "observed": obs,
                    "simulated": A.is_simulated(cp)}
    etag = a["preconditions"].get("etag")
    if "dateTime" in args.get("start", {}):
        _conflict_check(cp, a, args["start"]["dateTime"], args["end"]["dateTime"], ignore_event_id=eid)
    try:
        cp.patch_event(cal, eid, {k: v for k, v in args.items() if k in ("summary", "description", "start", "end", "location")},
                       etag)
    except ProviderError as e:
        if e.kind == "precondition_failed":
            raise Blocked("conflict", "Event changed on Google Calendar since the plan was made; re-propose.") from e
        raise
    ev = cp.get_event(cal, eid)
    ok, obs = _check_event(ev, args) if ev else (False, {})
    a.update(_ext_id=eid, _etag=ev.etag if ev else None, _url=ev.html_link if ev else None)
    return {"ok": ok, "method": "read_after_write", "external_id": eid, "etag": ev.etag if ev else None, "observed": obs,
            "simulated": A.is_simulated(cp), "link": _event_link, "message": f"Event updated: {obs.get('summary')}"}


def h_event_delete(conn, a: dict, reconcile_first: bool) -> dict:
    cp = A.calendar_provider(conn)
    eid, cal = a["args"]["event_id"], a["target_resource"]
    cur = cp.get_event(cal, eid)
    if cur is not None and cur.status != "cancelled":
        if cur.attendees and not cur.organizer_self:
            raise Blocked("failed", "Refusing to delete an event organized by someone else.")
        try:
            cp.delete_event(cal, eid, a["preconditions"].get("etag"))
        except ProviderError as e:
            if e.kind == "precondition_failed":
                raise Blocked("conflict", "Event changed since the plan was made; re-propose.") from e
            raise
    after = cp.get_event(cal, eid)
    ok = after is None or after.status == "cancelled"
    a.update(_ext_id=eid, _link_status="deleted")
    return {"ok": ok, "method": "read_after_write", "external_id": eid, "observed": {"exists": not ok},
            "simulated": A.is_simulated(cp), "link": _event_link, "message": "Event deleted from Google Calendar"}


def h_file(conn, a: dict, reconcile_first: bool) -> dict:
    from jarvis.localops import files
    return files.execute_action(conn, a, reconcile_first)




def pause(conn: sqlite3.Connection, reason: str | None) -> dict:
    with tx(conn):
        conn.execute("UPDATE control_state SET actions_paused=1, paused_at=?, paused_reason=? WHERE id=1", (now_iso(), reason))
        # Queued writes are held and their approvals revoked: resume never replays them automatically.
        held = conn.execute("SELECT json_extract(payload,'$.action_id') aid, json_extract(payload,'$.approval_id') apid "
                            "FROM jobs WHERE kind='execute_action' AND status='queued'").fetchall()
        for h in held:
            conn.execute("UPDATE proposed_actions SET state='paused', last_error='Held by Pause Actions; re-approve to run.', "
                         "updated_at=? WHERE id=? AND state='queued'", (now_iso(), h["aid"]))
            conn.execute("UPDATE approvals SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now_iso(), h["apid"]))
        conn.execute("UPDATE jobs SET status='paused', updated_at=? WHERE kind='execute_action' AND status='queued'", (now_iso(),))
        for pid in {r["pid"] for r in conn.execute(
                "SELECT DISTINCT v.proposal_id pid FROM proposed_actions a JOIN proposal_versions v ON v.id=a.proposal_version_id "
                "WHERE a.state='paused'").fetchall()}:
            # Reopen the proposal for a fresh decision (same version, new approval required).
            conn.execute("UPDATE proposed_actions SET state='proposed' WHERE state='paused' AND proposal_version_id IN "
                         "(SELECT id FROM proposal_versions WHERE proposal_id=?)", (pid,))
            conn.execute("UPDATE proposals SET status='proposed', updated_at=? WHERE id=?", (now_iso(), pid))
        audit(conn, "user", "control.paused", f"Actions paused ({len(held)} queued write(s) held)", data={"reason": reason})
    return control_view(conn)


def resume(conn: sqlite3.Connection) -> dict:
    with tx(conn):
        conn.execute("UPDATE control_state SET actions_paused=0, resumed_at=? WHERE id=1", (now_iso(),))
        audit(conn, "user", "control.resumed", "Actions resumed (held approvals are not replayed)")
    return control_view(conn)


def control_view(conn) -> dict:
    c = row(conn, "SELECT * FROM control_state WHERE id=1") or {}
    in_flight = [dict(r) for r in conn.execute(
        "SELECT id action_id, expected_effect summary, state FROM proposed_actions WHERE state IN ('executing','unknown')")]
    return {"actions_paused": bool(c.get("actions_paused")), "paused_at": c.get("paused_at"), "reason": c.get("paused_reason"),
            "in_flight": in_flight,
            "note": "Operations listed as executing/unknown may already have reached the provider." if in_flight else None}
