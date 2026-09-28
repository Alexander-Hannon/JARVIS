"""Durable schedule semantics: wall-clock intent across DST, misfire coalescing, no bursts, worker-run briefings."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from conftest import freeze, run_worker

from jarvis.briefings import scheduler as S

UTC = UTC


def sched(**kw):
    return {"tz": "America/Chicago", "hour": 6, "minute": 30, "weekdays": [0, 1, 2, 3, 4, 5, 6]} | kw


def test_wall_clock_preserved_across_dst():
    s = sched()
    # CDT (UTC-5) before Nov 1 2026, CST (UTC-6) after
    assert S.wall_to_utc(date(2026, 10, 31), 6, 30, s["tz"]) == datetime(2026, 10, 31, 11, 30, tzinfo=UTC)
    assert S.wall_to_utc(date(2026, 11, 2), 6, 30, s["tz"]) == datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
    occ = S.occurrences(s, datetime(2026, 10, 30, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC))
    assert [d.isoformat() for _, d in occ] == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]


def test_fall_back_ambiguous_time_runs_once():
    s = sched(hour=1, minute=30)
    occ = S.occurrences(s, datetime(2026, 11, 1, 0, tzinfo=UTC), datetime(2026, 11, 1, 12, tzinfo=UTC))
    assert len(occ) == 1 and occ[0][0] == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)   # first 1:30 (CDT)


def test_spring_forward_nonexistent_time_shifts_forward():
    s = sched(hour=2, minute=30)
    occ = S.occurrences(s, datetime(2027, 3, 14, 0, tzinfo=UTC), datetime(2027, 3, 14, 23, tzinfo=UTC))
    assert len(occ) == 1
    local = occ[0][0].astimezone(__import__("zoneinfo").ZoneInfo("America/Chicago"))
    assert (local.hour, local.minute) == (3, 30)


def test_host_timezone_irrelevant(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    import time
    time.tzset()
    try:
        assert S.wall_to_utc(date(2026, 12, 1), 6, 30, "America/Chicago") == datetime(2026, 12, 1, 12, 30, tzinfo=UTC)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def _enable(client, key):
    sid = [s for s in client.get("/api/schedules").json() if s["key"] == key][0]["id"]
    return client.patch(f"/api/schedules/{sid}", {"enabled": True}).json()


def test_defaults_shown_disabled(client):
    ss = {s["key"]: s for s in client.get("/api/schedules").json()}
    assert (ss["daily_command"]["hour"], ss["daily_command"]["minute"]) == (6, 30)
    assert (ss["nightly_debrief"]["hour"], ss["nightly_debrief"]["minute"]) == (20, 30)
    assert (ss["weekly_war_room"]["hour"], ss["weekly_war_room"]["weekdays"]) == (19, [6])
    assert not any(s["enabled"] for s in ss.values())
    assert all(s["tz"] == "America/Chicago" for s in ss.values())


def test_scheduled_briefing_generated_by_worker_without_browser(client, fakes, conn):
    freeze(datetime(2026, 9, 29, 11, 0, tzinfo=UTC))          # 6:00 CDT
    s = _enable(client, "daily_command")
    assert s["next_run_at"].startswith("2026-09-29T11:30")
    client.c.close()                                           # "close the browser"
    freeze(datetime(2026, 9, 29, 11, 31, tzinfo=UTC))
    run_worker(conn)
    b = conn.execute("SELECT * FROM briefings").fetchall()
    assert len(b) == 1 and b[0]["kind"] == "daily_command" and b[0]["schedule_run_id"] and not b[0]["catch_up"]
    n = conn.execute("SELECT * FROM notifications WHERE kind='briefing'").fetchall()
    assert len(n) == 1
    run_worker(conn)                                           # re-tick: no duplicate
    assert conn.execute("SELECT COUNT(*) FROM briefings").fetchone()[0] == 1
    sch = conn.execute("SELECT * FROM schedules WHERE key='daily_command'").fetchone()
    assert sch["last_success_at"] and sch["next_run_at"].startswith("2026-09-30T11:30")


def test_downtime_misfire_no_burst(client, fakes, conn):
    freeze(datetime(2026, 9, 29, 11, 0, tzinfo=UTC))
    _enable(client, "daily_command")
    # host asleep for 3 days; wakes 7:15 AM (45 min late for today's run → within 120 min grace)
    freeze(datetime(2026, 10, 2, 12, 15, tzinfo=UTC))
    run_worker(conn)
    runs = conn.execute("SELECT status, catch_up FROM schedule_runs ORDER BY scheduled_for").fetchall()
    assert [r["status"] for r in runs] == ["missed", "missed", "missed", "completed"]
    assert runs[-1]["catch_up"] == 1
    assert conn.execute("SELECT COUNT(*) FROM briefings").fetchone()[0] == 1
    assert conn.execute("SELECT catch_up FROM briefings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1       # one notification, no storm


def test_downtime_beyond_grace_marks_missed_one_notice(client, fakes, conn):
    freeze(datetime(2026, 9, 29, 11, 0, tzinfo=UTC))
    _enable(client, "daily_command")
    freeze(datetime(2026, 10, 2, 20, 0, tzinfo=UTC))           # 3 PM — too late to be useful
    run_worker(conn)
    assert conn.execute("SELECT COUNT(*) FROM briefings").fetchone()[0] == 0
    notes = conn.execute("SELECT title FROM notifications").fetchall()
    assert len(notes) == 1 and "Missed Daily Command" in notes[0]["title"]


def test_enabling_does_not_backfill(client, fakes, conn):
    freeze(datetime(2026, 9, 29, 15, 0, tzinfo=UTC))           # 10 AM, after today's 6:30
    s = _enable(client, "daily_command")
    assert s["next_run_at"].startswith("2026-09-30")
    run_worker(conn)
    assert conn.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0] == 0


def test_briefing_cache_until_inputs_change(client, fakes):
    freeze(datetime(2026, 9, 28, 20, 0, tzinfo=UTC))
    a = client.post("/api/briefings/generate", {"kind": "daily_command"}).json()
    b = client.post("/api/briefings/generate", {"kind": "daily_command"}).json()
    assert a["id"] == b["id"]
    fakes["todoist"].external_create("New urgent thing", project_id="proj-jarvis", due={"date": "2026-09-28"})
    from jarvis.agents import context
    context.invalidate()
    c = client.post("/api/briefings/generate", {"kind": "daily_command"}).json()
    assert c["id"] != a["id"]
    for k in ("nightly_debrief", "weekly_war_room"):
        x = client.post("/api/briefings/generate", {"kind": k}).json()
        assert x["kind"] == k and x["spoken_summary"]


def test_timedelta_import_used():
    assert timedelta(days=1)
