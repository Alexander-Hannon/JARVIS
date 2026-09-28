from __future__ import annotations

import pytest

from jarvis.core.util import new_id, now_iso
from jarvis.knowledge.ingest import delete_document, ingest_bytes
from jarvis.knowledge.search import build_match_query, search


def _project(conn, slug, sensitive):
    pid = new_id("prj")
    conn.execute("INSERT INTO projects (id, slug, name, area, sensitive, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 (pid, slug, slug, "coaching", 1 if sensitive else 0, now_iso(), now_iso()))
    return pid


@pytest.mark.parametrize("q", ['"unbalanced', "NEAR(a b)", "regress*", "-foo", "a AND OR NOT", "col:val",
                               "^start", "((", '""', "' OR 1=1 --", "*", "NEAR", "résumé naïve"])
def test_special_chars_do_not_error(conn, q):
    ingest_bytes(conn, b"regression diagnostics near the start", "a.txt", None, None)
    assert isinstance(search(conn, q), list)


def test_query_builder_quotes_tokens():
    assert build_match_query('NEAR("x" y*) -z') == '"near" "x" "y" "z"'
    assert build_match_query("a b", "OR") == '"a" OR "b"'
    assert build_match_query("!!!") is None


def test_search_returns_bounded_excerpt_and_fields(conn, gen):
    r = ingest_bytes(conn, gen.markdown_doc(), "notes.md", None, None)
    hits = search(conn, "problem set friday")
    assert hits
    h = hits[0]
    assert h["document_id"] == r["document_id"] and h["title"] == "notes.md"
    assert h["locator"] == "section 2: Deadlines" and h["kind"] == "text"
    assert h["segment_id"] and isinstance(h["score"], float)
    assert "[" in h["excerpt"] and len(h["excerpt"].split()) <= 40


def test_or_fallback(conn):
    ingest_bytes(conn, b"only regression here", "a.txt", None, None)
    assert search(conn, "regression unicorns")


def test_sensitive_project_excluded_unless_allowed(conn):
    secret = _project(conn, "coaching-client", True)
    normal = _project(conn, "school", False)
    ingest_bytes(conn, b"client quarterly goals zebra", "client.txt", None, secret)
    ingest_bytes(conn, b"school goals zebra", "school.txt", None, normal)
    titles = {h["title"] for h in search(conn, "zebra")}
    assert titles == {"school.txt"}
    assert {h["title"] for h in search(conn, "zebra", allowed_project_ids={secret})} == {"school.txt", "client.txt"}
    assert {h["title"] for h in search(conn, "zebra", project_id=secret)} == {"client.txt"}
    assert {h["title"] for h in search(conn, "zebra", exclude_sensitive_projects=False)} == {"school.txt", "client.txt"}


def test_deleted_and_old_versions_excluded(conn):
    r = ingest_bytes(conn, b"alpha giraffe", "v.txt", None, None)
    ingest_bytes(conn, b"beta okapi", "v.txt", None, None)
    assert not search(conn, "giraffe")  # superseded version
    assert search(conn, "okapi")
    delete_document(conn, r["document_id"])
    assert not search(conn, "okapi")
