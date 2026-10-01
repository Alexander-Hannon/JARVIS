"""V4 skills: install from a local folder (validated, copied), progressive loading as deferred tools, sandbox copy of
helper files, drafts installed only after AJ's explicit approval."""
from __future__ import annotations

import pytest
from conftest import CHICAGO_AFTERNOON, freeze, run_worker

from jarvis.llm.provider import ScriptedLLM, set_provider

SKILL = "---\nname: weekly-report\ndescription: How AJ formats his weekly GTA report\n---\n\nUse three headings: Done, Next, Blockers."


def agent(client, text, *calls):
    llm = ScriptedLLM([{"content": [{"type": "tool_use", "id": f"t{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)],
                        "stop_reason": "tool_use"}, {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}])
    set_provider(llm)
    r = client.post("/api/commands", {"conversation_id": None, "text": text, "captured_at": "2026-09-28T20:00:00Z",
                                      "tz": "America/Chicago"})
    assert r.status_code == 200, r.text
    return r.json(), llm


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "weekly"
    d.mkdir()
    (d / "SKILL.md").write_text(SKILL)
    (d / "fmt.py").write_text("print('formatted')")
    return d


def test_install_validation(client, tmp_path, folder):
    assert client.post("/api/skills/install", {"folder": str(tmp_path / "nope")}).json()["detail"]["code"] == "not_found"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter")
    assert client.post("/api/skills/install", {"folder": str(bad)}).json()["detail"]["code"] == "bad_frontmatter"
    (folder / "evil.bin").write_bytes(b"\x00\x01")
    assert client.post("/api/skills/install", {"folder": str(folder)}).json()["detail"]["code"] == "file_type"


def test_skill_loads_progressively_and_copies_helpers_to_sandbox(client, folder):
    from jarvis.sandbox import runtime as R
    rt = R.FakeRuntime()
    R.set_runtime(rt)
    try:
        s = client.post("/api/skills/install", {"folder": str(folder)}).json()
        assert s["name"] == "weekly-report" and s["files"] == ["SKILL.md", "fmt.py"]
        (folder / "SKILL.md").write_text(SKILL.replace("three", "NINE"))        # installed copy is unaffected
        r, llm = agent(client, "write my weekly report", ("skill__weekly_report", {}))
        tool = next(t for t in llm.requests[0]["tools"] if t.get("name") == "skill__weekly_report")
        assert tool["defer_loading"] is True and "Use three" not in tool["description"]      # body only on demand
        out = {b["tool_use_id"]: b["content"] for b in llm.requests[1]["messages"][-1]["content"]}["t0"]
        assert "Use three headings" in out and "/workspace/.skills/weekly-report/fmt.py" in out
        assert rt.files["/workspace/.skills/weekly-report/fmt.py"] == b"print('formatted')"
        client.post("/api/skills/weekly-report/enabled", {"enabled": False})
        _, llm = agent(client, "x", ("get_today_overview", {}))
        assert "skill__weekly_report" not in {t.get("name") for t in llm.requests[0]["tools"]}
    finally:
        R.set_runtime(None)


def test_drafted_skill_needs_explicit_approval(client, conn):
    freeze(CHICAGO_AFTERNOON)
    r, _ = agent(client, "remember how I like my report", ("propose_skill", {"name": "Report Style",
                                                                               "description": "AJ's report style",
                                                                               "instructions": "Be brief."}))
    p = r["proposal"]
    assert p["items"][0]["action_type"] == "skill.install" and p["items"][0]["args"]["name"] == "report-style"
    assert client.post("/api/policies", {"action_type": "skill.install", "label": "x"}).json()["detail"]["code"] == "not_t1"
    assert client.get("/api/skills").json() == []
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    assert [s["name"] for s in client.get("/api/skills").json()] == ["report-style"]
