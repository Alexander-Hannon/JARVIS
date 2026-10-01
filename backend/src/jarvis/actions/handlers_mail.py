"""Executor handlers for mail actions (Gmail / Outlook). Called only by actions/executor.py after every guard passed.

Drafts never contact anyone. Sending (`<box>.send`) is T2, needs AJ's per-message approval plus a `mail_send` write
grant that only exists after he enabled sending for that mailbox — and it is never re-sent automatically.
"""
from __future__ import annotations

import re

from jarvis.actions.executor import Blocked
from jarvis.integrations import accounts as A
from jarvis.integrations.base import ProviderError

LABEL = {"gmail": "Gmail", "outlook": "Outlook"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _check(d, args: dict, reply: bool) -> tuple[bool, dict]:
    obs = {"to": d.to, "subject": d.subject, "body_start": (d.body_text or "")[:160]}
    ok = {a.lower() for a in d.to} >= {a.lower() for a in args["to"]}
    if not reply:
        ok = ok and _norm(d.subject) == _norm(args["subject"])
    body = _norm(args["body"])[:80]
    return ok and body in _norm(d.body_text), obs


def h_draft_create(conn, a: dict, reconcile_first: bool) -> dict:
    box = a["provider"]
    mp = A.mail_provider(conn, box)
    args = a["args"]
    reply_id = args.get("reply_to_message_id")
    sim = A.is_simulated(mp)
    if reconcile_first:
        found = mp.find_draft_by_op(a["op_key"])
        if found:
            ok, obs = _check(found, args, bool(reply_id))
            return {"ok": ok, "method": "reconcile_marker", "external_id": found.id, "url": found.web_url, "simulated": sim,
                    "provider_ids": {"draft_id": found.id}, "observed": obs,
                    "message": f"Found the {LABEL[box]} draft already saved (no duplicate): {found.subject}"}
    reply = None
    if reply_id:
        reply = mp.get_message(reply_id)
        if reply is None:
            raise Blocked("conflict", "The email you're replying to no longer exists; nothing was drafted.")
    d = mp.create_draft(a["op_key"], args["to"], args["subject"], args["body"], cc=args.get("cc") or None, reply_to=reply)
    fetched = mp.get_draft(d.id)
    if fetched is None:
        raise ProviderError("server", "Created draft could not be read back.", outcome_known=False)
    ok, obs = _check(fetched, args, bool(reply_id))
    return {"ok": ok, "method": "read_after_write", "external_id": fetched.id, "url": fetched.web_url, "simulated": sim,
            "provider_ids": {"draft_id": fetched.id}, "observed": obs, "summary": {"draft_id": fetched.id},
            "message": f"Draft saved in {LABEL[box]} (not sent): {fetched.subject}"}


def h_draft_delete(conn, a: dict, reconcile_first: bool) -> dict:
    mp = A.mail_provider(conn, a["provider"])
    did = a["args"]["draft_id"]
    if mp.get_draft(did) is not None:
        mp.delete_draft(did)
    gone = mp.get_draft(did) is None
    return {"ok": gone, "method": "read_after_write", "external_id": did, "simulated": A.is_simulated(mp),
            "observed": {"exists": not gone}, "message": f"{LABEL[a['provider']]} draft deleted"}


def h_send(conn, a: dict, reconcile_first: bool) -> dict:
    box = a["provider"]
    mp = A.mail_provider(conn, box)
    did = a["args"]["draft_id"]
    draft = mp.get_draft(did)
    if reconcile_first:
        # Sending can't be made idempotent. A vanished draft means it went out (or AJ removed it); a remaining one
        # means we can't know — AJ decides. Mail is never re-sent automatically.
        if draft is None:
            return {"ok": True, "method": "reconcile_draft_gone", "external_id": did, "simulated": A.is_simulated(mp),
                    "observed": {"draft_exists": False}, "message": f"{LABEL[box]} draft is gone — treated as sent"}
        raise Blocked("failed", "The send outcome is unclear and the draft is still there. Review it and approve again.")
    if draft is None:
        raise Blocked("conflict", "That draft no longer exists; nothing was sent.")
    approved = {x.lower() for x in a["args"].get("to") or []}
    if approved and {x.lower() for x in draft.to} != approved:
        raise Blocked("conflict", "The draft's recipients changed after you approved sending it; nothing was sent.")
    sent_id = mp.send_draft(did)
    return {"ok": mp.get_draft(did) is None, "method": "read_after_write", "external_id": sent_id,
            "simulated": A.is_simulated(mp), "observed": {"to": draft.to, "subject": draft.subject},
            "provider_ids": {"message_id": sent_id}, "message": f"Sent from {LABEL[box]}: {draft.subject}"}
