"""Dedicated worker process: job leases, schedule ticks, heartbeat. Runs without any browser open.
The host must be awake and online; a sleeping laptop runs nothing (catch-up policy applies on wake)."""
from __future__ import annotations

import logging
import os
import signal
import socket
import time

from jarvis.actions import executor
from jarvis.actions import proposals as P
from jarvis.briefings import notify as N
from jarvis.briefings import scheduler, workflows
from jarvis.core import jobs
from jarvis.core.db import connection, jload, row, tx
from jarvis.core.util import audit, new_id, now_iso

log = logging.getLogger("jarvis.worker")
_stop = False


def handle_job(conn, job: dict, worker_id: str) -> None:
    kind, payload = job["kind"], job["payload"]
    try:
        if kind == "execute_action":
            res = executor.execute(conn, payload["action_id"], payload["approval_id"], job["id"])
            st = res.get("state")
            if st in ("queued", "unknown") and res.get("retryable"):
                delay = res.get("retry_after") or (15 * (2 ** (job["attempts"] - 1)))
                if job["attempts"] >= job["max_attempts"]:
                    with tx(conn):
                        if st == "queued":
                            conn.execute("UPDATE proposed_actions SET state='failed', updated_at=? WHERE id=?",
                                         (now_iso(), payload["action_id"]))
                        a = row(conn, "SELECT proposal_version_id FROM proposed_actions WHERE id=?", (payload["action_id"],))
                        v = row(conn, "SELECT proposal_id FROM proposal_versions WHERE id=?", (a["proposal_version_id"],))
                        P.recompute_status(conn, v["proposal_id"])
                jobs.fail(conn, job, worker_id, res.get("error") or st, retry=True, delay_s=min(delay, 600))
                return
            jobs.complete(conn, job["id"], worker_id, res)
        elif kind == "run_briefing":
            b = workflows.generate(conn, payload["workflow"], force=False, schedule_run_id=payload["schedule_run_id"],
                                   catch_up=payload.get("catch_up", False), scheduled=True)
            scheduler.complete_run(conn, payload["schedule_run_id"], True)
            jobs.complete(conn, job["id"], worker_id, {"briefing_id": b["id"]})
        elif kind == "send_push":
            jobs.complete(conn, job["id"], worker_id, N.send_push(conn, payload["notification_id"]))
        elif kind == "ics_refresh":
            from jarvis.integrations import service
            jobs.complete(conn, job["id"], worker_id, service.ics_refresh(conn))
        elif kind == "todoist_sync":
            from jarvis.integrations import service
            jobs.complete(conn, job["id"], worker_id, service.todoist_sync(conn))
        elif kind == "vault_index":
            from jarvis.knowledge import vault
            jobs.complete(conn, job["id"], worker_id, vault.index_root(conn, payload["root_id"]))
        else:
            jobs.fail(conn, job, worker_id, f"unknown job kind {kind}", retry=False)
    except Exception as e:  # visible failure, bounded retries
        log.exception("job %s failed", job["id"])
        if kind == "run_briefing":
            try:
                scheduler.complete_run(conn, payload["schedule_run_id"], False)
            except Exception:
                pass
        jobs.fail(conn, job, worker_id, f"{type(e).__name__}: {e}", retry=True, delay_s=60)


def heartbeat(conn, worker_id: str, started: str) -> None:
    conn.execute("INSERT INTO worker_heartbeats (worker_id, started_at, last_beat_at, pid, host) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(worker_id) DO UPDATE SET last_beat_at=excluded.last_beat_at",
                 (worker_id, started, now_iso(), os.getpid(), socket.gethostname()))


def run_once(conn, worker_id: str, max_jobs: int = 20) -> int:
    n = 0
    scheduler.tick(conn)
    with tx(conn):
        P.expire_stale(conn)
    while n < max_jobs:
        job = jobs.lease(conn, worker_id)
        if not job:
            break
        handle_job(conn, job, worker_id)
        n += 1
    return n


def maybe_periodic(conn, last: dict) -> None:
    """Low-frequency data refresh — no model calls, no polling agents."""
    now = time.monotonic()
    from jarvis.integrations import accounts as A
    if now - last.get("ics", 0) > 3600 and A.get_account(conn, "ics"):
        jobs.enqueue(conn, "ics_refresh", {}, dedupe_key=f"ics:{int(time.time() // 3600)}")
        last["ics"] = now
    if now - last.get("vault", 0) > 1800:
        from jarvis.knowledge import vault
        for rid in vault.indexed_roots(conn):
            jobs.enqueue(conn, "vault_index", {"root_id": rid}, dedupe_key=f"vault:{rid}:{int(time.time() // 1800)}")
        last["vault"] = now
    if now - last.get("todoist", 0) > 900 and A.get_account(conn, "todoist"):
        jobs.enqueue(conn, "todoist_sync", {}, dedupe_key=f"tsync:{int(time.time() // 900)}")
        last["todoist"] = now


def main(poll_seconds: float = 5.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from jarvis.logsafe import configure
    configure()
    worker_id = new_id("wkr")
    started = now_iso()

    def stop(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with connection() as conn:
        from jarvis.core.seed import seed
        with tx(conn):
            seed(conn)
        audit(conn, "worker", "worker.started", f"Worker {worker_id} started (pid {os.getpid()})")
        log.info("worker %s started", worker_id)
        last: dict = {}
        while not _stop:
            try:
                heartbeat(conn, worker_id, started)
                maybe_periodic(conn, last)
                run_once(conn, worker_id)
            except Exception:
                log.exception("worker loop error")
            time.sleep(poll_seconds)
        audit(conn, "worker", "worker.stopped", f"Worker {worker_id} stopped")


def status(conn) -> dict:
    hb = row(conn, "SELECT * FROM worker_heartbeats ORDER BY last_beat_at DESC LIMIT 1")
    if not hb:
        return {"alive": False, "last_beat_at": None, "worker_id": None, "pid": None,
                "note": "Worker not running: scheduled briefings and approved actions will not execute. Start `jarvis worker`."}
    from jarvis.core.util import parse_iso, utcnow
    alive = (utcnow() - parse_iso(hb["last_beat_at"])).total_seconds() < 60
    return {"alive": alive, "last_beat_at": hb["last_beat_at"], "worker_id": hb["worker_id"], "pid": hb["pid"],
            "note": None if alive else "Worker heartbeat is stale (stopped, or host was asleep)."}


def _j(x):
    return jload(x)
