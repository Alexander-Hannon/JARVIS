"""Structured, inspectable memory.

Authority order: user direct statement/edit (newest wins) > current project document > seed context >
agent suggestion (never a user decision). Equal-authority non-user conflicts are both kept with
status 'conflict' until AJ resolves them. Forget removes the searchable FTS rows and scrubs version
text locally; historical backups may still contain it until they are deleted.
"""
from __future__ import annotations

import re
import sqlite3

from jarvis.core.db import row, rows, tx
from jarvis.core.util import audit, new_id, now_iso

AUTHORITY = {"user_edit": 4, "user_statement": 4, "document": 3, "seed": 2, "agent_suggestion": 1}
_STOP = set(["a", "an", "the", "i", "my", "me", "to", "of", "and", "or", "in", "on", "at", "for", "is", "are", "be", "that", "this", "it", "do", "does", "with", "as", "by", "from", "about", "remember", "note", "please", "jarvis"])


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def containment(a: str, b: str) -> tuple[float, int]:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0, 0
    n = len(ta & tb)
    return n / min(len(ta), len(tb)), n


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _fts_query(q: str) -> str:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q) if t.lower() not in _STOP][:12]
    return " OR ".join(f'"{t}"' for t in toks)


def _insert_version(conn, mid: str, version: int, content: str, source_kind: str, source_ref: str | None,
                    review_at: str | None, expires_at: str | None, effective_from: str | None = None) -> None:
    conn.execute("INSERT INTO memory_versions (id, memory_id, version, content, source_kind, source_ref, observed_at, "
                 "effective_from, review_at, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (new_id("mv"), mid, version, content, source_kind, source_ref, now_iso(), effective_from, review_at,
                  expires_at, now_iso()))
    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (mid,))
    conn.execute("INSERT INTO memory_fts (content, memory_id) VALUES (?,?)", (content, mid))


def add(conn: sqlite3.Connection, content: str, type_: str, source_kind: str, *, source_ref: str | None = None,
        project_id: str | None = None, entity: str | None = None, pinned: bool = False, review_at: str | None = None,
        expires_at: str | None = None, effective_from: str | None = None, supersede_similar: bool = True) -> dict:
    """Create a memory. Returns {id, superseded:[ids], conflicts:[ids]}."""
    content = content.strip()
    confirmed = source_kind in ("user_statement", "user_edit", "seed")
    status = "hypothesis" if source_kind == "agent_suggestion" else "active"
    if source_kind == "agent_suggestion":
        type_ = "hypothesis"
    mid = new_id("mem")
    now = now_iso()
    superseded, conflicts = [], []
    candidates = []
    if supersede_similar and source_kind != "agent_suggestion":
        candidates = similar(conn, content, project_id=project_id, entity=entity)
    conn.execute("INSERT INTO memories (id, project_id, entity, type, status, pinned, confirmed_by_user, scope, created_at, "
                 "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (mid, project_id, entity, type_, status, int(pinned), int(confirmed), "project" if project_id else "global",
                  now, now))
    _insert_version(conn, mid, 1, content, source_kind, source_ref, review_at, expires_at, effective_from)
    for c in candidates:
        if c["type"] != type_ and not entity:
            continue
        new_auth, old_auth = AUTHORITY.get(source_kind, 1), AUTHORITY.get(c["source_kind"], 1)
        if new_auth >= 4 or new_auth > old_auth:
            conn.execute("UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?", (mid, now, c["id"]))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (c["id"],))
            superseded.append(c["id"])
            # the replacement inherits identity (entity/project/pin) so every agent finds the current version
            conn.execute("UPDATE memories SET entity=COALESCE(entity, ?), project_id=COALESCE(project_id, ?), "
                         "pinned=MAX(pinned, ?) WHERE id=?", (c["entity"], c["project_id"], c["pinned"], mid))
        elif new_auth == old_auth:
            conn.execute("UPDATE memories SET status='conflict', conflict_with=?, updated_at=? WHERE id IN (?,?)",
                         (c["id"], now, mid, c["id"]))
            conn.execute("UPDATE memories SET conflict_with=? WHERE id=?", (mid, c["id"]))
            conflicts.append(c["id"])
    audit(conn, "user" if confirmed else "model", "memory.created", content[:120], "memory", mid,
          {"superseded": superseded, "conflicts": conflicts, "source_kind": source_kind})
    return {"id": mid, "superseded": superseded, "conflicts": conflicts}


def similar(conn, content: str, project_id: str | None = None, entity: str | None = None, threshold: float = 0.4) -> list[dict]:
    if entity:
        return rows(conn, "SELECT m.*, v.content, v.source_kind FROM memories m JOIN memory_versions v ON v.memory_id=m.id "
                          "AND v.version=m.current_version WHERE m.entity=? AND m.status IN ('active','conflict')", (entity,))
    q = _fts_query(content)
    if not q:
        return []
    cands = rows(conn, "SELECT m.*, v.content, v.source_kind FROM memory_fts f JOIN memories m ON m.id=f.memory_id "
                       "JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version "
                       "WHERE memory_fts MATCH ? AND m.status IN ('active','conflict') "
                       "AND (m.project_id IS ? OR m.project_id IS NULL) ORDER BY bm25(memory_fts) LIMIT 10", (q, project_id))
    out = []
    for c in cands:
        cont, n = containment(c["content"], content)
        if similarity(c["content"], content) >= threshold or (cont >= 0.5 and n >= 3):
            out.append(c)
    return out


def correct(conn: sqlite3.Connection, memory_id: str, new_content: str, source_kind: str = "user_edit") -> dict:
    with tx(conn):
        m = row(conn, "SELECT * FROM memories WHERE id=?", (memory_id,))
        if not m or m["status"] == "forgotten":
            raise KeyError(memory_id)
        v = m["current_version"] + 1
        _insert_version(conn, memory_id, v, new_content.strip(), source_kind, None, None, None)
        conn.execute("UPDATE memories SET current_version=?, status='active', confirmed_by_user=1, conflict_with=NULL, "
                     "type=CASE WHEN type='hypothesis' THEN 'preference' ELSE type END, updated_at=? WHERE id=?",
                     (v, now_iso(), memory_id))
        if m["conflict_with"]:
            # AJ's correction resolves the conflict in favor of this record.
            conn.execute("UPDATE memories SET status='superseded', superseded_by=?, conflict_with=NULL, updated_at=? WHERE id=?",
                         (memory_id, now_iso(), m["conflict_with"]))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (m["conflict_with"],))
        audit(conn, "user", "memory.corrected", f"v{v}: {new_content[:100]}", "memory", memory_id)
    return get(conn, memory_id)


def forget(conn: sqlite3.Connection, memory_id: str) -> None:
    with tx(conn):
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        conn.execute("UPDATE memory_versions SET content='[forgotten]', source_ref=NULL WHERE memory_id=?", (memory_id,))
        conn.execute("UPDATE memories SET status='forgotten', pinned=0, updated_at=? WHERE id=?", (now_iso(), memory_id))
        audit(conn, "user", "memory.forgotten", "Memory forgotten (search index + text removed)", "memory", memory_id)


def set_pinned(conn, memory_id: str, pinned: bool) -> None:
    conn.execute("UPDATE memories SET pinned=?, updated_at=? WHERE id=?", (int(pinned), now_iso(), memory_id))


def get(conn, memory_id: str, with_versions: bool = True) -> dict | None:
    m = row(conn, "SELECT m.*, v.content, v.source_kind, v.source_ref, v.observed_at, v.review_at, v.expires_at, "
                  "v.effective_from FROM memories m JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version "
                  "WHERE m.id=?", (memory_id,))
    if m and with_versions:
        m["versions"] = rows(conn, "SELECT version, content, source_kind, observed_at FROM memory_versions WHERE memory_id=? "
                                   "ORDER BY version", (memory_id,))
    if m:
        m["version"] = m["current_version"]
        m["pinned"], m["confirmed_by_user"] = bool(m["pinned"]), bool(m["confirmed_by_user"])
    return m


def list_(conn, q: str | None = None, status: str | None = None, project_id: str | None = None, limit: int = 200) -> list[dict]:
    params: list = []
    base = ("SELECT m.*, v.content, v.source_kind, v.source_ref, v.observed_at, v.review_at, v.expires_at FROM memories m "
            "JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version")
    where = []
    if q:
        fq = _fts_query(q)
        if not fq:
            return []
        base += " JOIN memory_fts f ON f.memory_id=m.id"
        where.append("memory_fts MATCH ?")
        params.append(fq)
    if status:
        where.append("m.status=?")
        params.append(status)
    else:
        where.append("m.status!='forgotten'")
    if project_id:
        where.append("m.project_id=?")
        params.append(project_id)
    sql = base + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.pinned DESC, m.updated_at DESC LIMIT ?"
    params.append(limit)
    out = rows(conn, sql, params)
    for m in out:
        m["version"] = m["current_version"]
        m["pinned"], m["confirmed_by_user"] = bool(m["pinned"]), bool(m["confirmed_by_user"])
    return out


def relevant(conn, text: str, project_ids: list[str] | None = None, limit: int = 8) -> list[dict]:
    """Bounded context for a run: pinned + FTS matches, active only, never other sensitive projects."""
    pinned = rows(conn, "SELECT m.id, m.type, m.project_id, v.content, v.source_kind, v.observed_at FROM memories m "  # nosec B608 fixed clauses; values bound
                        "JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version "
                        "WHERE m.pinned=1 AND m.status='active' AND (m.project_id IS NULL OR m.project_id IN "
                        f"({','.join('?' * len(project_ids or []))})) LIMIT ?", [*(project_ids or []), limit])
    hits = []
    fq = _fts_query(text)
    if fq:
        hits = rows(conn, "SELECT m.id, m.type, m.project_id, v.content, v.source_kind, v.observed_at FROM memory_fts f "
                          "JOIN memories m ON m.id=f.memory_id JOIN memory_versions v ON v.memory_id=m.id AND "
                          "v.version=m.current_version WHERE memory_fts MATCH ? AND m.status IN ('active','conflict') "
                          "ORDER BY bm25(memory_fts) LIMIT ?", (fq, limit))
        allowed = set(project_ids or [])
        sensitive = {r["id"] for r in rows(conn, "SELECT id FROM projects WHERE sensitive=1")}
        hits = [h for h in hits if not h["project_id"] or h["project_id"] in allowed or h["project_id"] not in sensitive]
    seen, out = set(), []
    for m in pinned + hits:
        if m["id"] not in seen:
            seen.add(m["id"])
            out.append(m)
    return out[:limit]


def export_markdown(conn, ids: list[str] | None = None) -> str:
    items = list_(conn, limit=10000) if not ids else [get(conn, i, with_versions=False) for i in ids]
    lines = ["# Jarvis memory export", ""]
    for m in items:
        if not m:
            continue
        lines.append(f"- **{m['type']}** ({m['status']}{', pinned' if m['pinned'] else ''}) — {m['content']}  ")
        lines.append(f"  _source: {m['source_kind']}{' · ' + m['source_ref'] if m.get('source_ref') else ''} · "
                     f"observed {m['observed_at'][:10]} · v{m['current_version']}_")
    return "\n".join(lines) + "\n"
