"""Durable wall-clock schedules (IANA tz), evaluated by the worker — never by a browser tab.

- next_run: the next local wall time HH:MM on an allowed weekday. Nonexistent local times (spring
  forward) run at the first valid instant after the gap; ambiguous times (fall back) run once, at the
  first occurrence. The per-(schedule, local date) dedupe key makes a second run impossible.
- Misfires (host asleep/offline): only the most recent missed occurrence is considered. Within its
  grace window it runs once as a labeled catch-up; older/expired ones are marked missed with at most
  ONE summary notification — never a burst.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jarvis.briefings.notify import notify
from jarvis.core import jobs
from jarvis.core.db import jdump, jload, row, rows, tx
from jarvis.core.util import audit, iso, new_id, now_iso, parse_iso, utcnow

UTC = ZoneInfo("UTC")


def wall_to_utc(d: date, hh: int, mm: int, tz: str) -> datetime:
    zi = ZoneInfo(tz)
    naive = datetime.combine(d, time(hh, mm))
    # fold=0: ambiguous (fall-back) times resolve to the FIRST occurrence; nonexistent (spring-forward)
    # times use the pre-transition offset, i.e. they land shifted forward by the gap (02:30 -> 03:30 CDT).
    return naive.replace(tzinfo=zi, fold=0).astimezone(UTC)


def occurrences(sched: dict, start_utc: datetime, end_utc: datetime) -> list[tuple[datetime, date]]:
    """All (utc instant, local date) occurrences with start_utc < instant <= end_utc."""
    zi = ZoneInfo(sched["tz"])
    wds = set(jload(sched["weekdays"]) if isinstance(sched["weekdays"], str) else sched["weekdays"])
    d = start_utc.astimezone(zi).date() - timedelta(days=1)
    last = end_utc.astimezone(zi).date() + timedelta(days=1)
    out = []
    while d <= last:
        if d.weekday() in wds:
            inst = wall_to_utc(d, sched["hour"], sched["minute"], sched["tz"])
            if start_utc < inst <= end_utc:
                out.append((inst, d))
        d += timedelta(days=1)
    return out


def next_run(sched: dict, after_utc: datetime) -> datetime | None:
    occ = occurrences(sched, after_utc, after_utc + timedelta(days=8))
    return occ[0][0] if occ else None


def refresh_next(conn, sched_id: str, after: datetime | None = None) -> None:
    s = row(conn, "SELECT * FROM schedules WHERE id=?", (sched_id,))
    nxt = next_run(s, after or utcnow()) if s["enabled"] else None
    conn.execute("UPDATE schedules SET next_run_at=?, updated_at=? WHERE id=?", (iso(nxt) if nxt else None, now_iso(), sched_id))


def update_schedule(conn, sched_id: str, patch: dict) -> dict:
    with tx(conn):
        s = row(conn, "SELECT * FROM schedules WHERE id=?", (sched_id,))
        if not s:
            raise KeyError(sched_id)
        fields = {k: patch[k] for k in ("enabled", "hour", "minute", "destination") if k in patch}
        if "weekdays" in patch:
            fields["weekdays"] = jdump(sorted(set(int(x) for x in patch["weekdays"])))
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE schedules SET {sets}, row_version=row_version+1, updated_at=? WHERE id=?",  # nosec B608 columns allowlisted above; values bound
                         (*fields.values(), now_iso(), sched_id))
        # Activation starts from NOW: enabling never back-fills past occurrences.
        refresh_next(conn, sched_id)
        audit(conn, "user", "schedule.updated", f"{s['label']}: {fields}", "schedule", sched_id)
    return view(conn, sched_id)


def view(conn, sched_id: str) -> dict:
    s = row(conn, "SELECT * FROM schedules WHERE id=?", (sched_id,))
    s["weekdays"] = jload(s["weekdays"])
    s["enabled"] = bool(s["enabled"])
    return s


def tick(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Evaluate due schedules. Returns list of actions taken (for tests/diagnostics)."""
    now = now or utcnow()
    actions = []
    for s in rows(conn, "SELECT * FROM schedules WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=?", (iso(now),)):
        with tx(conn):
            s = row(conn, "SELECT * FROM schedules WHERE id=?", (s["id"],))
            if not s["enabled"] or not s["next_run_at"] or parse_iso(s["next_run_at"]) > now:
                continue
            since = parse_iso(s["last_run_at"]) if s["last_run_at"] else parse_iso(s["next_run_at"]) - timedelta(seconds=1)
            since = min(since, parse_iso(s["next_run_at"]) - timedelta(seconds=1))
            occ = occurrences(s, since, now)
            grace = timedelta(minutes=s["misfire_grace_minutes"])
            missed_old = occ[:-1]
            latest = occ[-1] if occ else None
            for inst, d in missed_old:
                _record_run(conn, s, inst, d, "missed", catch_up=False)
            if latest:
                inst, d = latest
                late = now - inst
                if late <= grace:
                    run_id = _record_run(conn, s, inst, d, "queued", catch_up=late > timedelta(minutes=5))
                    if run_id:
                        jid = jobs.enqueue(conn, "run_briefing", {"schedule_run_id": run_id, "workflow": s["workflow"],
                                                                  "catch_up": late > timedelta(minutes=5)},
                                           dedupe_key=f"brief:{s['key']}:{d.isoformat()}")
                        conn.execute("UPDATE schedule_runs SET job_id=? WHERE id=?", (jid, run_id))
                        actions.append({"schedule": s["key"], "date": d.isoformat(), "action": "queued",
                                        "catch_up": late > timedelta(minutes=5)})
                else:
                    if _record_run(conn, s, inst, d, "missed", catch_up=False):
                        actions.append({"schedule": s["key"], "date": d.isoformat(), "action": "missed"})
                        n_missed = len(occ)
                        notify(conn, "schedule_missed", f"Missed {s['label']}" + (f" (+{n_missed - 1} earlier)" if n_missed > 1 else ""),
                               "The host was asleep or offline. Generate it now from Briefings if still useful.",
                               "schedule", s["id"], push=False)
            nxt = next_run(s, now)
            conn.execute("UPDATE schedules SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
                         (iso(now), iso(nxt) if nxt else None, now_iso(), s["id"]))
    return actions


def _record_run(conn, s: dict, inst: datetime, d: date, status: str, catch_up: bool) -> str | None:
    rid = new_id("srun")
    cur = conn.execute("INSERT OR IGNORE INTO schedule_runs (id, schedule_id, scheduled_for, local_wall, dedupe_key, status, "
                       "catch_up, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (rid, s["id"], iso(inst), f"{d.isoformat()}T{s['hour']:02d}:{s['minute']:02d} {s['tz']}",
                        f"{s['key']}:{d.isoformat()}", status, int(catch_up), now_iso()))
    return rid if cur.rowcount else None


def complete_run(conn, schedule_run_id: str, ok: bool) -> None:
    r = row(conn, "SELECT * FROM schedule_runs WHERE id=?", (schedule_run_id,))
    conn.execute("UPDATE schedule_runs SET status=? WHERE id=?", ("completed" if ok else "failed", schedule_run_id))
    conn.execute("UPDATE schedules SET last_status=?, last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END WHERE id=?",
                 ("ok" if ok else "failed", int(ok), now_iso(), r["schedule_id"]))
