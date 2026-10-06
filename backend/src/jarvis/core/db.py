"""SQLite access: one connection per call site, WAL, foreign keys, forward-only migrations."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from jarvis.config import get_settings

_init_lock = threading.Lock()
_initialized: set[str] = set()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# A migration containing this line runs with foreign keys off (see migrate()).
FK_OFF_MARKER = "-- jarvis:foreign-keys-off"


def migration_files() -> list[tuple[int, str, str]]:
    out = []
    pkg = resources.files("jarvis.migrations")
    for entry in pkg.iterdir():
        name = entry.name
        if name.endswith(".sql"):
            out.append((int(name.split("_", 1)[0]), name, entry.read_text()))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for version, name, sql in migration_files():
        if version in applied:
            continue
        # Table rebuilds (SQLite can't alter a UNIQUE/CHECK in place) follow SQLite's documented procedure: foreign keys
        # off *outside* the transaction so DROP TABLE doesn't cascade-delete children, then foreign_key_check before
        # COMMIT so a rebuild that broke a reference aborts the whole migration.
        fk_off = FK_OFF_MARKER in sql
        if fk_off:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would commit implicitly; run statements one by one inside our transaction
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            if fk_off:
                bad = conn.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise sqlite3.IntegrityError(f"{name}: foreign key violations after rebuild: {bad[:5]}")
            conn.execute("INSERT INTO schema_migrations VALUES (?,?,datetime('now'))", (version, name))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            if fk_off:
                conn.execute("PRAGMA foreign_keys = ON")
        done.append(name)
    return done


def _split_sql(sql: str) -> list[str]:
    stmts, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";") and sqlite3.complete_statement("\n".join(buf)):
            stmts.append("\n".join(buf))
            buf = []
    if buf:
        stmts.append("\n".join(buf))
    return stmts


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def get_conn() -> sqlite3.Connection:
    s = get_settings()
    s.ensure_dirs()
    key = str(s.db_path)
    conn = _connect(s.db_path)
    if key not in _initialized:
        with _init_lock:
            if key not in _initialized:
                migrate(conn)
                _initialized.add(key)
    return conn


def reset_init_cache() -> None:
    _initialized.clear()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def row(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def jload(v: str | None, default: Any = None) -> Any:
    if v is None:
        return default
    return json.loads(v)


def jdump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)
