"""V4 sandbox: tools, network windows (T2), exports, computer-use loop, scope, and the egress proxy's allowlist logic.
The container runtime is a labeled FakeRuntime (Docker isn't available on this host); the model is SCRIPTED."""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest
from conftest import CHICAGO_AFTERNOON, freeze, run_worker

from jarvis.llm.provider import ScriptedLLM, set_provider
from jarvis.sandbox.runtime import ExecResult


@pytest.fixture
def rt():
    from jarvis.sandbox import runtime as R
    f = R.FakeRuntime()
    R.set_runtime(f)
    yield f
    R.set_runtime(None)


def tu(i, name, inp, **extra):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp, **extra}


def agent(client, text, *blocks):
    llm = ScriptedLLM([{"content": list(blocks), "stop_reason": "tool_use"},
                       {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}])
    set_provider(llm)
    r = client.post("/api/commands", {"conversation_id": None, "text": text, "captured_at": "2026-09-28T20:00:00Z",
                                      "tz": "America/Chicago"})
    assert r.status_code == 200, r.text
    return r.json(), llm


def results(llm):
    return {b["tool_use_id"]: b for b in llm.requests[1]["messages"][-1]["content"]}


def test_unavailable_docker_is_truthful_and_offers_nothing(client, monkeypatch):
    from jarvis.sandbox import runtime as R
    monkeypatch.setattr(R.shutil, "which", lambda _: None)
    R.set_runtime(R.DockerRuntime())
    try:
        st = client.get("/api/sandbox").json()
        assert st["available"] is False and "WSL integration" in st["detail"]
        _, llm = agent(client, "run some python", tu(0, "get_today_overview", {}))
        names = {t.get("name") or t.get("type") for t in llm.requests[0]["tools"]}
        assert "sandbox_run" not in names and "computer_toolset_20260801" not in names
        assert client.post("/api/sandbox/start").status_code == 409
    finally:
        R.set_runtime(None)


def test_run_write_read_and_history(client, rt):
    freeze(CHICAGO_AFTERNOON)
    rt.outputs["python3 calc.py"] = ExecResult(0, "42\n", "", 120)
    _, llm = agent(client, "compute it in the sandbox",
                   tu(0, "sandbox_write_file", {"path": "calc.py", "content": "print(6*7)"}),
                   tu(1, "sandbox_run", {"command": "python3 calc.py"}),
                   tu(2, "sandbox_read_file", {"path": "../../etc/passwd"}))
    res = results(llm)
    assert '"path": "/workspace/calc.py"' in res["t0"]["content"] and rt.files["/workspace/calc.py"] == b"print(6*7)"
    assert '"stdout": "42\\n"' in res["t1"]["content"] and "untrusted" in res["t1"]["content"]
    assert "inside /workspace" in res["t2"]["content"]
    hist = client.get("/api/sandbox/history").json()
    assert [h["kind"] for h in hist][:2] == ["exec", "write"] and hist[0]["detail"] == "python3 calc.py"


@pytest.mark.injection
def test_network_window_needs_explicit_approval_and_scopes_browser(client, conn, rt):
    freeze(CHICAGO_AFTERNOON)
    _, llm = agent(client, "open the parking page", tu(0, "browser_open", {"url": "https://www.uah.edu/parking"}))
    assert "propose_sandbox_network" in results(llm)["t0"]["content"]
    bad, llm = agent(client, "x", tu(0, "propose_sandbox_network", {"domains": ["http://evil.com/x", "10.0.0.1"], "why": "x"}))
    assert "plain domain names" in results(llm)["t0"]["content"] and bad["proposal"] is None
    r, _ = agent(client, "open the parking page", tu(0, "propose_sandbox_network",
                                                     {"domains": ["uah.edu"], "minutes": 20, "why": "Read parking page"}))
    p = r["proposal"]
    assert p["items"][0]["action_type"] == "sandbox.network" and p["approval"] is None
    assert client.post("/api/policies", {"action_type": "sandbox.network", "label": "x"}).json()["detail"]["code"] == "not_t1"
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    assert client.get("/api/sandbox").json()["network"]["domains"] == ["uah.edu"]
    client.post("/api/sandbox/start")                                   # stored window is pushed to the proxy on start
    assert rt.egress[0] == ["uah.edu"] and rt.egress[1] > CHICAGO_AFTERNOON.timestamp()
    rt.outputs["browse.py"] = ExecResult(0, json.dumps({"title": "Parking", "url": "https://www.uah.edu/parking",
                                                        "text": "Permits cost $90"}), "", 900)
    r, llm = agent(client, "open the parking page", tu(0, "browser_open", {"url": "https://www.uah.edu/parking"}),
                   tu(1, "browser_open", {"url": "https://evil.example/steal"}))
    res = results(llm)
    assert "Permits cost $90" in res["t0"]["content"] and "can't reach evil.example" in res["t1"]["content"]
    assert client.post("/api/sandbox/network/close").json()["active"] is False and rt.egress[1] == 0


def test_export_goes_through_file_write_proposal(client, conn, rt, tmp_path):
    freeze(CHICAGO_AFTERNOON)
    root_dir = tmp_path / "ws"
    root_dir.mkdir()
    client.post("/api/localops/roots", {"path": str(root_dir), "label": "Work", "writable": True})
    rt.files["/workspace/out/report.md"] = b"# Report\nAll good."
    r, _ = agent(client, "save the report to my folder", tu(0, "propose_export_file",
                                                             {"sandbox_path": "out/report.md", "dest_path": "Reports/report.md"}))
    item = r["proposal"]["items"][0]
    assert item["action_type"] == "file.write" and not (root_dir / "Reports" / "report.md").exists()
    p = r["proposal"]
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    assert (root_dir / "Reports" / "report.md").read_text() == "# Report\nAll good."


def test_computer_toolset_drives_sandbox_desktop(client, rt):
    freeze(CHICAGO_AFTERNOON)
    _, llm = agent(client, "fill in the form on the sandbox desktop",
                   tu(0, "screenshot", {}, toolset_name="computer"),
                   tu(1, "left_click", {"coordinate": [100, 200], "text": "ctrl"}, toolset_name="computer"),
                   tu(2, "type", {"text": "hello"}, toolset_name="computer"),
                   tu(3, "left_click", {"coordinate": [99999, 1]}, toolset_name="computer"),
                   tu(4, "key", {"text": "Return"}, toolset_name="computer"))
    assert {"type": "computer_toolset_20260801"} in llm.requests[0]["tools"]
    res = results(llm)
    assert all(r["toolset_name"] == "computer" for r in res.values())
    assert res["t0"]["content"][0]["type"] == "image" and res["t1"]["content"] == "OK"
    assert res["t3"]["is_error"] and res["t4"]["content"].startswith("Not executed") and res["t4"]["is_error"]
    assert rt.inputs[:5] == [["mousemove", "100", "200"], ["keydown", "ctrl"], ["click", "--repeat", "1", "1"],
                             ["keyup", "ctrl"], ["type", "--delay", "12", "--", "hello"]]
    assert not any(i[0] == "key" for i in rt.inputs)          # halted after the failure


def test_scope_without_sandbox_gets_no_desktop(client, rt):
    from jarvis.agents import agent as A
    from jarvis.core.db import get_conn
    c = get_conn()
    assert A.computer_tools(c, "usr_owner", A.Scope(families=frozenset({"email"}))) == []
    assert A.computer_tools(c, "usr_owner", A.COMMAND_SCOPE) == [{"type": "computer_toolset_20260801"}]
    client.post("/api/sandbox/settings", {"computer_use": False})
    assert A.computer_tools(c, "usr_owner", A.COMMAND_SCOPE) == []


def _proxy_module(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("egress_proxy", Path(__file__).parents[1] / "sandbox" / "egress_proxy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ALLOW", str(tmp_path / "allow.json"))
    return mod


def test_egress_proxy_allowlist(tmp_path, monkeypatch):
    m = _proxy_module(tmp_path, monkeypatch)
    assert not m.allowed("pypi.org")                                    # no file = closed
    (tmp_path / "allow.json").write_text(json.dumps({"domains": ["pypi.org"], "until": time.time() + 60}))
    assert m.allowed("pypi.org") and m.allowed("files.pypi.org") and not m.allowed("evilpypi.org")
    assert not m.allowed("pypi.org.evil.com")
    (tmp_path / "allow.json").write_text(json.dumps({"domains": ["pypi.org"], "until": time.time() - 1}))
    assert not m.allowed("pypi.org")                                    # expired window = closed


def test_large_files_are_never_silently_truncated(client, conn, rt):
    """Regression (found live): exec output is clipped for the model, so file reads use a binary path instead."""
    from jarvis.sandbox import service as S
    rt.files["/workspace/big.csv"] = ("x," * 20000).encode()
    assert len(S.read_file(conn, "usr_owner", None, "big.csv", 100000)) == 40000
    with pytest.raises(S.SandboxError) as e:
        S.read_file(conn, "usr_owner", None, "big.csv", 1000)
    assert e.value.code == "too_big"
