"""Durable SQLite job queue with leases.

- enqueue() is idempotent on dedupe_key (INSERT OR IGNORE) — the transactional-outbox pattern:
  callers enqueue inside the same transaction that records the approval.
- lease() atomically claims one ready job for a worker with an expiry. A crashed worker's lease
  expires and the job becomes claimable again; handlers must be safe to re-run (the executor
  reconciles actions left in 'executing' as unknown-outcome before any retry).
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from jarvis.core.db import jdump, jload, row, tx
from jarvis.core.util import iso, new_id, now_iso, utcnow


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict, dedupe_key: str | None = None,
            run_after: str | None = None, max_attempts: int = 3) -> str:
    jid = new_id("job")
    now = now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs (id, kind, payload, status, dedupe_key, run_after, max_attempts, created_at, updated_at)"
        " VALUES (?,?,?,'queued',?,?,?,?,?)",
        (jid, kind, jdump(payload), dedupe_key, run_after or now, max_attempts, now, now),
    )
    if cur.rowcount == 0 and dedupe_key:
        existing = row(conn, "SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,))
        return existing["id"]
    return jid


def lease(conn: sqlite3.Connection, worker_id: str, lease_seconds: int = 120, kinds: list[str] | None = None) -> dict | None:
    now = utcnow()
    with tx(conn):
        paused = row(conn, "SELECT actions_paused FROM control_state WHERE id=1")
        kind_filter = ""
        params: list = [iso(now), iso(now)]
        if kinds:
            kind_filter = f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if paused and paused["actions_paused"]:
            # While paused, never hand out provider-write jobs.
            kind_filter += " AND kind NOT IN ('execute_action')"
        cand = row(conn,
                   "SELECT * FROM jobs WHERE ((status='queued' AND run_after<=?) OR (status='leased' AND lease_expires_at<?))"
                   f"{kind_filter} ORDER BY run_after, created_at LIMIT 1", params)
        if not cand:
            return None
        conn.execute(
            "UPDATE jobs SET status='leased', lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=? WHERE id=?",
            (worker_id, iso(now + timedelta(seconds=lease_seconds)), iso(now), cand["id"]),
        )
        cand["attempts"] += 1
        cand["payload"] = jload(cand["payload"], {})
        cand["recovered_lease"] = cand["status"] == "leased"
        return cand


def complete(conn: sqlite3.Connection, job_id: str, worker_id: str, result: dict | None = None) -> None:
    conn.execute("UPDATE jobs SET status='done', result=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                 "WHERE id=? AND lease_owner=?", (jdump(result or {}), now_iso(), job_id, worker_id))


def fail(conn: sqlite3.Connection, job: dict, worker_id: str, error: str, retry: bool, delay_s: float = 30) -> None:
    if retry and job["attempts"] < job["max_attempts"]:
        conn.execute("UPDATE jobs SET status='queued', run_after=?, last_error=?, lease_owner=NULL, lease_expires_at=NULL, "
                     "updated_at=? WHERE id=? AND lease_owner=?",
                     (iso(utcnow() + timedelta(seconds=delay_s)), error[:2000], now_iso(), job["id"], worker_id))
    else:
        conn.execute("UPDATE jobs SET status=?, last_error=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                     "WHERE id=? AND lease_owner=?",
                     ("dead" if retry else "failed", error[:2000], now_iso(), job["id"], worker_id))


def cancel_where(conn: sqlite3.Connection, kind: str, status: str = "queued", new_status: str = "cancelled") -> int:
    return conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE kind=? AND status=?",
                        (new_status, now_iso(), kind, status)).rowcount
