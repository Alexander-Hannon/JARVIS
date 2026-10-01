"""Local spend ledger: atomic worst-case reservations, reconciliation with measured usage.

This is a local estimate/ledger, not a provider-side cap. Provider billing is authoritative.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from jarvis.core.db import jdump, jload, row, tx
from jarvis.core.util import new_id, now_iso, utcnow

# USD per 1M tokens (input, output). Checked 2026-09-28 against the claude-api skill pricing table.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_MULT = 0.1
WEB_SEARCH_PER_REQUEST = 0.01   # $10 per 1,000 searches (web fetch is billed as tokens only)
CACHE_WRITE_MULT = 1.25


class BudgetBlocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def budget_settings(conn) -> dict:
    data = jload((row(conn, "SELECT data FROM user_settings WHERE id=1") or {}).get("data"), {}) or {}
    return data.get("budget", {})


def update_budget(conn, patch: dict) -> dict:
    with tx(conn):
        r = row(conn, "SELECT data FROM user_settings WHERE id=1")
        data = jload(r["data"], {})
        b = data.get("budget", {})
        allowed = {"paid_inference_enabled", "background_paid_enabled", "daily_limit_usd", "monthly_limit_usd",
                   "max_output_tokens", "max_tool_calls", "max_specialists", "effort"}
        b.update({k: v for k, v in patch.items() if k in allowed})
        b["chosen"] = True
        data["budget"] = b
        conn.execute("UPDATE user_settings SET data=?, version=version+1, updated_at=? WHERE id=1", (jdump(data), now_iso()))
    return b


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float | None:
    p = PRICING.get(model)
    if not p:
        return None
    return (input_tokens * p[0] + output_tokens * p[1] + cache_read * p[0] * CACHE_READ_MULT
            + cache_write * p[0] * CACHE_WRITE_MULT) / 1_000_000


def _period_start(tz: str, monthly: bool) -> str:
    local = utcnow().astimezone(ZoneInfo(tz))
    start = local.replace(day=1 if monthly else local.day, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def spent(conn, since: str) -> tuple[float, float]:
    measured = row(conn, "SELECT COALESCE(SUM(cost_usd),0) s FROM usage_records WHERE created_at>=?", (since,))["s"]
    reserved = row(conn, "SELECT COALESCE(SUM(est_cost_usd),0) s FROM usage_reservations WHERE status='reserved' AND created_at>=?",
                   (since,))["s"]
    return float(measured), float(reserved)


def reserve(conn: sqlite3.Connection, purpose: str, model: str, est_input_tokens: int, max_output_tokens: int,
            scheduled: bool = False, run_id: str | None = None, max_web_searches: int = 0) -> str:
    with tx(conn):
        b = budget_settings(conn)
        if not b.get("chosen"):
            raise BudgetBlocked("budget_not_chosen", "Paid inference is off until you choose a budget (Settings → Budget).")
        if not b.get("paid_inference_enabled"):
            raise BudgetBlocked("paid_disabled", "Paid model inference is turned off in Settings → Budget.")
        if scheduled and not b.get("background_paid_enabled"):
            raise BudgetBlocked("background_disabled", "Paid background work is disabled; briefing generated without the model.")
        est = estimate_cost(model, est_input_tokens, max_output_tokens)
        if est is not None:
            est += max_web_searches * WEB_SEARCH_PER_REQUEST
        if est is None:
            if scheduled or b.get("daily_limit_usd") is not None or b.get("monthly_limit_usd") is not None:
                raise BudgetBlocked("pricing_unknown", f"No pricing configured for model '{model}'; paid call refused.")
            est = 0.0
        tz = row(conn, "SELECT timezone FROM user_settings WHERE id=1")["timezone"]
        for limit_key, monthly in (("daily_limit_usd", False), ("monthly_limit_usd", True)):
            lim = b.get(limit_key)
            if lim is None:
                continue
            m, r = spent(conn, _period_start(tz, monthly))
            if m + r + est > float(lim):
                raise BudgetBlocked("limit_reached", f"{'Monthly' if monthly else 'Daily'} model budget (${lim:.2f}) would be "
                                                     f"exceeded (spent ${m:.2f}, reserved ${r:.2f}). Capture, reads and drafts still work.")
        rid = new_id("res")
        conn.execute("INSERT INTO usage_reservations (id, run_id, purpose, model, est_cost_usd, status, scheduled, created_at, "
                     "updated_at) VALUES (?,?,?,?,?, 'reserved', ?,?,?)", (rid, run_id, purpose, model, est, int(scheduled),
                                                                           now_iso(), now_iso()))
        return rid


def reconcile(conn, reservation_id: str, model: str, usage: dict, request_id: str | None) -> float:
    cost = estimate_cost(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                         usage.get("cache_read_input_tokens", 0) or 0, usage.get("cache_creation_input_tokens", 0) or 0) or 0.0
    stu = usage.get("server_tool_use") or {}
    cost += (stu.get("web_search_requests") or 0) * WEB_SEARCH_PER_REQUEST
    with tx(conn):
        conn.execute("INSERT INTO usage_records (id, reservation_id, model, input_tokens, output_tokens, cache_read_tokens, "
                     "cache_write_tokens, cost_usd, measured, request_id, created_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                     (new_id("use"), reservation_id, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                      usage.get("cache_read_input_tokens", 0) or 0, usage.get("cache_creation_input_tokens", 0) or 0, cost,
                      request_id, now_iso()))
        conn.execute("UPDATE usage_reservations SET status='reconciled', updated_at=? WHERE id=?", (now_iso(), reservation_id))
    return cost


def release(conn, reservation_id: str) -> None:
    conn.execute("UPDATE usage_reservations SET status='released', updated_at=? WHERE id=?", (now_iso(), reservation_id))


def summary(conn) -> dict:
    b = budget_settings(conn)
    tz = row(conn, "SELECT timezone FROM user_settings WHERE id=1")["timezone"]
    dm, dr = spent(conn, _period_start(tz, False))
    mm, mr = spent(conn, _period_start(tz, True))
    return b | {"spent_today_usd": round(dm, 4), "reserved_today_usd": round(dr, 4), "spent_month_usd": round(mm, 4),
                "reserved_month_usd": round(mr, 4), "pricing": {k: {"input_per_mtok": v[0], "output_per_mtok": v[1]}
                                                                for k, v in PRICING.items()},
                "measured_vs_estimated": "Spent = measured provider usage fields × local price table; reserved = worst-case "
                                         "estimates for in-flight calls. Provider billing is authoritative; this is not a hard "
                                         "provider-side cap."}


def _now_local(tz: str) -> datetime:
    return utcnow().astimezone(ZoneInfo(tz))


def run_cost(conn, run_id: str) -> float:
    """Measured (or reserved, while in flight) spend attributed to one run — used for per-mission caps."""
    r = conn.execute("SELECT COALESCE(SUM(u.cost_usd),0) FROM usage_records u JOIN usage_reservations r ON r.id=u.reservation_id "
                     "WHERE r.run_id=?", (run_id,)).fetchone()[0]
    pending = conn.execute("SELECT COALESCE(SUM(est_cost_usd),0) FROM usage_reservations WHERE run_id=? AND status='reserved'",
                           (run_id,)).fetchone()[0]
    return float(r) + float(pending)
