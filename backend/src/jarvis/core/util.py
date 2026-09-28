"""Small shared helpers: IDs, UTC time, hashing, audit and activity events."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from jarvis.core.db import jdump

# Tests may freeze "now" by assigning a callable here.
_now_override: Any = None


def utcnow() -> datetime:
    if _now_override is not None:
        return _now_override()
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp: {s}")
    return dt


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def audit(conn: sqlite3.Connection, actor: str, kind: str, summary: str, entity_type: str | None = None,
          entity_id: str | None = None, data: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events (at, actor, kind, entity_type, entity_id, summary, data) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), actor, kind, entity_type, entity_id, summary, jdump(data or {})),
    )


def run_event(conn: sqlite3.Connection, run_id: str | None, kind: str, message: str, data: dict | None = None) -> None:
    """Concise user-visible activity. Never store model chain-of-thought here."""
    conn.execute(
        "INSERT INTO run_events (run_id, kind, message, data, created_at) VALUES (?,?,?,?,?)",
        (run_id, kind, message, jdump(data or {}), now_iso()),
    )


def record_latency(conn: sqlite3.Connection, kind: str, ms: float, meta: dict | None = None) -> None:
    conn.execute("INSERT INTO latency_metrics (kind, ms, meta, at) VALUES (?,?,?,?)",
                 (kind, int(ms), jdump(meta or {}), now_iso()))
