"""FTS5 evidence search with a safe query builder and sensitive-project boundaries.

User text is never passed to FTS5 as query syntax: it is tokenized here and each token is
emitted as a double-quoted string, so operators like NEAR, AND/OR/NOT, *, -, ^, :, and
parentheses are treated as literal words (or dropped when they are punctuation).
Results are bounded excerpts of untrusted document content, returned as data.
"""
from __future__ import annotations

import re
import sqlite3

MAX_TOKENS = 24
MAX_TOKEN_LEN = 64
_TOKEN = re.compile(r"\w+", re.UNICODE)


def build_match_query(q: str, mode: str = "AND") -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH expression, or None if nothing searchable."""
    seen: list[str] = []
    for t in _TOKEN.findall(q or ""):
        t = t[:MAX_TOKEN_LEN].lower()
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= MAX_TOKENS:
            break
    if not seen:
        return None
    joiner = " OR " if mode == "OR" else " "
    return joiner.join('"' + t.replace('"', '""') + '"' for t in seen)


def search(conn: sqlite3.Connection, q: str, project_id: str | None = None, limit: int = 10,
           exclude_sensitive_projects: bool = True, allowed_project_ids: set | None = None) -> list[dict]:
    """Rank current-version text segments by bm25. AND semantics first, OR fallback for recall.

    Sensitive projects (projects.sensitive=1) are excluded unless the project is explicitly
    requested via project_id, listed in allowed_project_ids, or exclusion is turned off.
    """
    limit = max(1, min(int(limit), 50))
    for mode in ("AND", "OR"):
        mq = build_match_query(q, mode)
        if mq is None:
            return []
        res = _run(conn, mq, project_id, limit, exclude_sensitive_projects, set(allowed_project_ids or ()))
        if res:
            return res
    return []


def _run(conn: sqlite3.Connection, mq: str, project_id: str | None, limit: int, exclude_sensitive: bool,
         allowed: set) -> list[dict]:
    where = ["evidence_fts MATCH ?", "d.deleted_at IS NULL", "v.id = d.current_version_id"]
    params: list = [mq]
    if project_id is not None:
        where.append("d.project_id = ?")
        params.append(project_id)
        allowed = allowed | {project_id}
    if exclude_sensitive:
        allowed_list = sorted(allowed)
        clause = "(d.project_id IS NULL OR COALESCE(p.sensitive, 0) = 0"
        if allowed_list:
            clause += f" OR d.project_id IN ({','.join('?' * len(allowed_list))})"
            params.extend(allowed_list)
        where.append(clause + ")")
    sql = f"""
        SELECT s.id AS segment_id, s.kind, s.locator, d.id AS document_id, d.title, d.project_id,
               v.id AS version_id, v.version,
               snippet(evidence_fts, 0, '[', ']', '…', 30) AS excerpt,
               bm25(evidence_fts) AS rank
        FROM evidence_fts
        JOIN evidence_segments s ON s.id = evidence_fts.segment_id
        JOIN source_versions v ON v.id = s.source_version_id
        JOIN source_documents d ON d.id = v.document_id
        LEFT JOIN projects p ON p.id = d.project_id
        WHERE {' AND '.join(where)}
        ORDER BY rank
        LIMIT ?"""  # nosec B608 clauses/placeholders from code; values bound
    params.append(limit)
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["score"] = round(-float(d.pop("rank")), 6)
        d["untrusted"] = True
        out.append(d)
    return out
