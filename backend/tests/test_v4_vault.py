"""Obsidian/Markdown vault indexing on a real temporary folder (no fakes needed)."""
from __future__ import annotations

from conftest import run_worker


def _vault(tmp_path):
    v = tmp_path / "Vault"
    (v / "School").mkdir(parents=True)
    (v / ".obsidian").mkdir()
    (v / "School" / "IS 640 rubric.md").write_text("# Rubric\nLate work loses 10 percent per day. See [[Syllabus]].")
    (v / "Ideas.md").write_text("Business idea: tutoring marketplace for GTAs.")
    (v / ".obsidian" / "workspace.md").write_text("internal obsidian state zebra")
    (v / "secrets.md").write_text("api password zebra")          # secret-looking name: never read
    (v / "pic.png").write_bytes(b"\x89PNG")                        # not an indexed type
    return v


def _search(client, q):
    return client.get("/api/search", params={"q": q}).json()


def test_index_search_update_and_forget(client, conn, tmp_path):
    v = _vault(tmp_path)
    root = client.post("/api/localops/roots", {"path": str(v), "label": "Vault", "writable": False}).json()
    st = client.post(f"/api/vault/roots/{root['id']}", {"enabled": True}).json()
    assert st["indexed_roots"] == [root["id"]]
    run_worker(conn)
    last = client.get("/api/vault").json()["last_index"][root["id"]]
    assert last["new_or_changed"] == 2 and last["removed"] == 0
    hits = _search(client, "late work")
    assert any("rubric" in (h.get("title") or "").lower() for h in hits)
    assert not _search(client, "zebra")                               # hidden folder + secret file never indexed

    # unchanged files dedupe; edits re-version; deletions are forgotten
    (v / "Ideas.md").write_text("Business idea: AI study planner.")
    (v / "School" / "IS 640 rubric.md").unlink()
    client.post(f"/api/vault/roots/{root['id']}/reindex")
    run_worker(conn)
    last = client.get("/api/vault").json()["last_index"][root["id"]]
    assert last == last | {"new_or_changed": 1, "removed": 1, "unchanged": 0}
    assert not _search(client, "late work") and _search(client, "study planner")

    # turning indexing off removes the notes from search
    client.post(f"/api/vault/roots/{root['id']}", {"enabled": False})
    assert not _search(client, "study planner")


def test_removing_root_forgets_its_notes(client, conn, tmp_path):
    v = _vault(tmp_path)
    root = client.post("/api/localops/roots", {"path": str(v), "label": "Vault", "writable": False}).json()
    client.post(f"/api/vault/roots/{root['id']}", {"enabled": True})
    run_worker(conn)
    assert _search(client, "tutoring")
    client.delete(f"/api/localops/roots/{root['id']}")
    assert not _search(client, "tutoring")
    assert client.post(f"/api/vault/roots/{root['id']}/reindex").status_code == 404
