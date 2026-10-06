"""Personal (per-user) and org-wide settings.

`user_settings` holds one row per user: timezone + personal preferences (voice, privacy, notifications, research,
location, address_as, todoist areas, default mailbox, vault roots, disabled skills, computer use).
`org_settings` is the install-wide row: brand, models, budget limits (admins edit it).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from jarvis.core.db import jdump, jload, row
from jarvis.core.util import now_iso

ORG_KEYS = ("budget", "models", "brand")
DEFAULT_TZ = "UTC"


def user_row(conn: sqlite3.Connection, uid: str) -> dict:
    return row(conn, "SELECT * FROM user_settings WHERE user_id=?", (uid,)) or {"user_id": uid, "timezone": DEFAULT_TZ,
                                                                                "data": "{}", "version": 0}


def user_data(conn: sqlite3.Connection, uid: str) -> dict:
    return jload(user_row(conn, uid).get("data"), {}) or {}


def user_tz(conn: sqlite3.Connection, uid: str) -> str:
    return user_row(conn, uid).get("timezone") or DEFAULT_TZ


def update_user_data(conn: sqlite3.Connection, uid: str, fn: Callable[[dict], Any]) -> dict:
    """Read-modify-write a user's settings JSON (`fn` mutates the dict in place). Caller owns the transaction."""
    data = user_data(conn, uid)
    fn(data)
    conn.execute("INSERT INTO user_settings (user_id, timezone, data, updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, version=version+1, updated_at=excluded.updated_at",
                 (uid, DEFAULT_TZ, jdump(data), now_iso()))
    return data


def org_data(conn: sqlite3.Connection) -> dict:
    return jload((row(conn, "SELECT data FROM org_settings WHERE id=1") or {}).get("data"), {}) or {}


def update_org_data(conn: sqlite3.Connection, fn: Callable[[dict], Any]) -> dict:
    data = org_data(conn)
    fn(data)
    conn.execute("INSERT INTO org_settings (id, data, updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET "
                 "data=excluded.data, version=version+1, updated_at=excluded.updated_at", (jdump(data), now_iso()))
    return data


def display_name(conn: sqlite3.Connection, uid: str) -> str:
    r = row(conn, "SELECT display_name FROM users WHERE id=?", (uid,))
    return r["display_name"] if r else "there"
