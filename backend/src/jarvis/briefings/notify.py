"""In-app notification inbox + Web Push delivery.

Delivery states are distinct: generated (row exists) → queued (push job) → accepted (push service
returned 201) → opened (client reported click). "Accepted" is NOT proof AJ saw it.
Lock-screen payload is minimal by default (title only + generic body) unless details are opted in.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from jarvis.config import get_settings
from jarvis.core import jobs
from jarvis.core.db import jload, row, rows
from jarvis.core.util import new_id, now_iso

log = logging.getLogger("jarvis.notify")


def notify(conn: sqlite3.Connection, kind: str, title: str, body: str, ref_type: str | None = None,
           ref_id: str | None = None, push: bool | None = None) -> str:
    nid = new_id("ntf")
    conn.execute("INSERT INTO notifications (id, kind, title, body, ref_type, ref_id, created_at) VALUES (?,?,?,?,?,?,?)",
                 (nid, kind, title[:200], body[:4000], ref_type, ref_id, now_iso()))
    settings = jload((row(conn, "SELECT data FROM user_settings WHERE id=1") or {}).get("data"), {}) or {}
    want_push = push if push is not None else settings.get("notifications", {}).get("push_kinds", {}).get(kind, kind in (
        "briefing", "action_result"))
    if want_push:
        if row(conn, "SELECT 1 FROM push_subscriptions WHERE revoked_at IS NULL"):
            conn.execute("UPDATE notifications SET push_status='queued' WHERE id=?", (nid,))
            jobs.enqueue(conn, "send_push", {"notification_id": nid}, dedupe_key=f"push:{nid}")
        else:
            conn.execute("UPDATE notifications SET push_status='no_subscription' WHERE id=?", (nid,))
    if push is not False:   # Telegram follows its own toggle; an explicit push=False (quiet hours) silences it too
        from jarvis.channels import telegram
        telegram.maybe_forward_notification(conn, nid, kind)
    return nid


def push_config() -> dict:
    s = get_settings()
    if not (s.vapid_public_key and s.vapid_private_key):
        return {"supported": False, "vapid_public_key": None,
                "reason": "VAPID keys not configured. Run `uv run jarvis gen-vapid` and set JARVIS_VAPID_* in .env. "
                          "Push also requires HTTPS (or localhost) and, on iPhone, an installed home-screen web app."}
    return {"supported": True, "vapid_public_key": s.vapid_public_key}


def send_push(conn: sqlite3.Connection, notification_id: str) -> dict:
    s = get_settings()
    n = row(conn, "SELECT * FROM notifications WHERE id=?", (notification_id,))
    if not n:
        return {"status": "missing"}
    if not (s.vapid_public_key and s.vapid_private_key):
        conn.execute("UPDATE notifications SET push_status='failed' WHERE id=?", (notification_id,))
        return {"status": "not_configured"}
    from pywebpush import WebPushException, webpush

    settings = jload((row(conn, "SELECT data FROM user_settings WHERE id=1") or {}).get("data"), {}) or {}
    details = settings.get("notifications", {}).get("lock_screen_details", False)
    payload = json.dumps({"title": n["title"], "body": n["body"][:180] if details else "Open Jarvis for details.",
                          "id": n["id"], "url": f"/?notification={n['id']}"})
    accepted = 0
    for sub in rows(conn, "SELECT * FROM push_subscriptions WHERE revoked_at IS NULL"):
        try:
            webpush(subscription_info={"endpoint": sub["endpoint"], "keys": jload(sub["keys"])}, data=payload,
                    vapid_private_key=s.vapid_private_key, vapid_claims={"sub": s.vapid_subject}, ttl=3600)
            accepted += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                conn.execute("UPDATE push_subscriptions SET revoked_at=?, last_error=? WHERE id=?",
                             (now_iso(), f"gone ({code})", sub["id"]))
            else:
                conn.execute("UPDATE push_subscriptions SET last_error=? WHERE id=?", (str(e)[:300], sub["id"]))
    status = "accepted" if accepted else "failed"
    conn.execute("UPDATE notifications SET push_status=?, push_accepted_at=? WHERE id=?",
                 (status, now_iso() if accepted else None, notification_id))
    return {"status": status, "accepted": accepted}
