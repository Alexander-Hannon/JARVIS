"""V5 M1a isolation sweep: two people on one install can never see or act on each other's data.

User A (owner) gets one of every kind of object, each carrying a marker string. User B then calls EVERY registered
route — as a member and again as an admin — with A's ids substituted into the path. B must get 404 for A's objects,
no response may contain A's markers or object ids, and afterwards A's data is unchanged.

A route with a path parameter that isn't covered by BODIES/ID_FOR below fails the test, so new routes can't skip it.
"""
from __future__ import annotations

import re

import pytest
from conftest import LOOPBACK, OWNER, PASSWORD, Api
from fastapi.testclient import TestClient

MARK = "ZEBRAMARKER"


def _flatten():
    from jarvis.api import routes, users, v4
    out = []
    for r in routes.common.routes + routes.router.routes + v4.router.routes + users.router.routes:
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((m, r.path))
    return out


@pytest.fixture
def world(client, fakes, conn, tmp_path, monkeypatch):
    """A's objects (via the API where possible, so they're created the way real ones are)."""
    from jarvis.api import v4
    from jarvis.briefings.notify import notify
    from jarvis.sandbox import runtime as R
    from jarvis.sandbox import service as SB
    monkeypatch.setattr(v4, "STREAM_WAIT_FOR_RUN", 0.2)
    rt = R.FakeRuntime()
    R.set_runtime(rt)
    a = client
    ids: dict[str, str] = {}
    r = a.post("/api/commands", {"text": f"add a task to buy {MARK} milk tomorrow", "captured_at": "2026-09-28T20:00:00Z",
                                 "tz": "America/Chicago"}).json()
    ids |= {"conv": r["conversation_id"], "run": r["run_id"], "proposal": r["proposal"]["id"],
            "proposal_hash": r["proposal"]["hash"], "capture": r["message"]["capture_id"]}
    ids["memory"] = a.post("/api/memories", {"content": f"{MARK} memory: allergic to peanuts"}).json()["id"]
    up = a.c.post("/api/documents", files={"file": ("notes.txt", f"{MARK} private doc about Q3 layoffs".encode(), "text/plain")},
                  headers={"X-CSRF-Token": a.csrf}).json()
    ids["doc"] = up["document_id"]
    ids["mission"] = a.post("/api/missions", {"name": f"{MARK} mission", "goal": f"Watch {MARK}",
                                              "schedule": {"kind": "daily", "at": "07:00"}, "families": []}).json()["id"]
    ids["rule"] = a.post("/api/policies", {"action_type": "task.create", "label": f"{MARK} rule"}).json()["id"]
    conn.execute("BEGIN")
    ids["notification"] = notify(conn, OWNER, "mission", f"{MARK} notification", f"{MARK} body", push=False)
    conn.execute("COMMIT")
    ids["voice"] = a.post("/api/voice/sessions", {"mode": "push_to_talk"}).json()["id"]
    import time

    from jarvis.security.mfa import totp_at
    t = a.post("/api/auth/mfa/totp/begin", {"label": f"{MARK} phone"}).json()
    assert a.post("/api/auth/mfa/totp/confirm", {"id": t["id"], "code": totp_at(t["secret"], int(time.time() // 30))}
                  ).status_code == 200
    ids["mfa"] = t["id"]
    ids["project"] = conn.execute("SELECT id FROM projects WHERE user_id=? LIMIT 1", (OWNER,)).fetchone()[0]
    ids["schedule"] = conn.execute("SELECT id FROM schedules WHERE user_id=? LIMIT 1", (OWNER,)).fetchone()[0]
    ids["session"] = conn.execute("SELECT id FROM sessions WHERE user_id=? AND revoked_at IS NULL", (OWNER,)).fetchone()[0]
    ids["account"] = fakes["todoist_account"]
    root_dir = tmp_path / f"{MARK}-root"
    root_dir.mkdir()
    (root_dir / "plan.md").write_text(f"{MARK} secret plan")
    ids["root"] = a.post("/api/localops/roots", {"path": str(root_dir), "label": f"{MARK} folder"}).json()["id"]
    conn.execute("BEGIN")
    ids["shot"] = SB._save_shot(OWNER, b"\x89PNG\r\n\x1a\n" + MARK.encode())
    SB._log(conn, OWNER, ids["run"], "exec", f"echo {MARK}")
    conn.execute("COMMIT")
    # an approved + verified action, so there is something to "undo"
    v = a.get(f"/api/proposals/{ids['proposal']}").json()
    ids["action"] = v["items"][0]["action_id"]
    yield ids
    R.set_runtime(None)


def _make_user(owner: Api, role: str, username: str) -> Api:
    inv = owner.post("/api/users", {"username": username, "display_name": username.title(), "role": role})
    assert inv.status_code == 200, inv.text
    token = inv.json()["invite_url"].rsplit("/", 1)[-1]
    c = TestClient(owner.c.app, client=("203.0.113.50", 41000))
    r = c.post("/api/auth/invite/accept", json={"token": token, "password": "bea password 1234"})
    assert r.status_code == 200, r.text
    return Api(c, r.json()["csrf_token"])


def _id_for(path: str, ids: dict) -> str:
    """Which of A's ids to put into a route's path parameter."""
    if path.startswith("/api/conversations/"):
        return ids["conv"]
    if path.startswith("/api/memories/"):
        return ids["memory"]
    if path.startswith("/api/missions/"):
        return ids["mission"]
    if path.startswith("/api/projects/"):
        return ids["project"]
    if path.startswith(("/api/proposals/", "/api/approvals/co/")):
        return ids["proposal"]
    if path.startswith("/api/schedules/"):
        return ids["schedule"]
    if path.startswith("/api/auth/sessions/"):
        return ids["session"]
    if path.startswith("/api/auth/mfa/"):
        return ids["mfa"]
    table = {"/api/integrations/{account_id}": "account", "/api/actions/": "action", "/api/captures/": "capture",
             "/api/documents/": "doc", "/api/notifications/": "notification", "/api/localops/roots/": "root",
             "/api/vault/roots/": "root", "/api/policies/": "rule", "/api/runs/": "run", "/api/sandbox/shots/": "shot",
             "/api/voice/sessions/": "voice"}
    for prefix, key in table.items():
        if path.startswith(prefix.split("{")[0]):
            return ids[key]
    if path.startswith("/api/users/"):
        return OWNER
    # org-level objects (not A's personal data): connectors catalog ids, MCP servers, skills, backups, Drive file ids
    return {"/api/connectors/": "notion", "/api/mcp/": "mcp_doesnotexist", "/api/skills/": "nosuchskill",
            "/api/backups/": "bk_doesnotexist", "/api/integrations/drive/": "drivefile"}[
        next(p for p in ("/api/connectors/", "/api/mcp/", "/api/skills/", "/api/backups/", "/api/integrations/drive/")
             if path.startswith(p))]


BODIES = {
    ("PATCH", "/api/memories/{mid}"): {"content": "overwritten"}, ("POST", "/api/memories/{mid}/pin"): {"pinned": False},
    ("PATCH", "/api/missions/{mid}"): {"name": "hijacked"}, ("POST", "/api/missions/{mid}/enabled"): {"enabled": False},
    ("PATCH", "/api/projects/{pid}"): {"description": "hijacked", "visibility": "org"},
    ("POST", "/api/proposals/{pid}/retry"): {"items": [0]},
    ("POST", "/api/proposals/{pid}/revise"): {"items": [{"index": 0, "remove": True}]},
    ("PATCH", "/api/schedules/{sid}"): {"enabled": True}, ("POST", "/api/captures/{cap_id}/transcript"): {"text": "hijacked"},
    ("POST", "/api/documents/{doc_id}/visibility"): {"visibility": "org"},
    ("POST", "/api/vault/roots/{root_id}"): {"enabled": True}, ("PATCH", "/api/users/{uid}"): {"display_name": "hijacked"},
    ("POST", "/api/connectors/{cid}/connect"): {"values": {}}, ("POST", "/api/connectors/{cid}/resources"): {},
    ("POST", "/api/mcp/{sid}/review"): {"modes": {}}, ("POST", "/api/mcp/{sid}/enabled"): {"enabled": True},
    ("POST", "/api/skills/{name}/enabled"): {"enabled": False}, ("POST", "/api/backups/{bid}/restore"): {"confirm": True},
}
# Routes on org-level objects where B legitimately gets a non-404 answer about *their own* state (never A's).
ORG_LEVEL = {"/api/connectors/{cid}/connect", "/api/connectors/{cid}/resources", "/api/skills/{name}/enabled",
             "/api/integrations/drive/ingest/{file_id}"}
# Admin-managed host folders: an admin may remove any user's root (it's an assignment, not content).
ADMIN_MANAGED = {("DELETE", "/api/localops/roots/{rid}")}
# Reads that deliberately answer "nothing" for an unknown run instead of 404 (the UI polls before the run exists).
EMPTY_OK = {("GET", "/api/runs/{run_id}/events"), ("GET", "/api/runs/{run_id}/stream")}


def _call(api: Api, method: str, path: str, body):
    if method == "GET":
        return api.get(path)
    if method == "DELETE":
        return api.delete(path)
    if method == "PATCH":
        return api.patch(path, body or {})
    return api.post(path, body or {})


def _leaks(text: str, ids: dict) -> list[str]:
    found = [MARK] if MARK in text else []
    return found + [k for k, v in ids.items() if k not in ("proposal_hash",) and isinstance(v, str) and len(v) > 6 and v in text]


@pytest.mark.parametrize("role", ["member", "admin"])
def test_other_user_cannot_see_or_touch_anything(client, world, conn, role):
    b = _make_user(client, role, f"bea{role}")
    ids = world
    failures = []
    for method, path in _flatten():
        if path in ("/api/auth/logout",) or (method, path) in (("POST", "/api/auth/sessions/revoke-others"),):
            continue       # would end B's own session mid-sweep (scoped to B; covered in test_v5_security)
        params = re.findall(r"\{(\w+)\}", path)
        url = path
        for p in params:
            url = url.replace("{" + p + "}", _id_for(path, ids))
        if (method, path) == ("POST", "/api/approvals/co/{pid}"):
            body = {"version": 1, "hash": ids["proposal_hash"]}
        elif (method, path) == ("POST", "/api/proposals/{pid}/approve"):
            body = {"version": 1, "hash": ids["proposal_hash"], "selected_items": [0]}
        else:
            body = BODIES.get((method, path), {})
        if method == "GET" and params == [] and path in ("/api/sandbox/live.png",):
            pass
        r = _call(b, method, url, body)
        text = r.text if not r.headers.get("content-type", "").startswith("image/") else ""
        leaked = _leaks(text, ids)
        if leaked:
            failures.append(f"{method} {path} leaked {leaked}: {text[:160]}")
            continue
        if not params or path in ORG_LEVEL:
            continue
        if (method, path) in ADMIN_MANAGED and role == "admin":
            continue
        if (method, path) in EMPTY_OK and r.status_code == 200:
            continue
        ok = {404} | ({403} if r.status_code == 403 else set())
        if r.status_code not in ok and not (path.startswith(("/api/mcp/", "/api/backups/", "/api/skills/")) and
                                            r.status_code in (400, 403, 404)):
            failures.append(f"{method} {path} → {r.status_code} (expected 404): {text[:160]}")
    assert not failures, "\n".join(failures)
    # A's world is untouched
    a = client
    assert a.get("/api/memories").json()[0]["content"].startswith(MARK) or any(
        m["content"].startswith(MARK) for m in a.get("/api/memories").json())
    assert a.get(f"/api/proposals/{ids['proposal']}").json()["status"] == "proposed"
    doc = a.get(f"/api/documents/{ids['doc']}").json()
    assert doc["visibility"] == "private" and not doc["deleted_at"]
    m = next(x for x in a.get("/api/missions").json()["missions"] if x["id"] == ids["mission"])
    assert m["enabled"] and m["name"].startswith(MARK)
    assert any(r["id"] == ids["rule"] and r["active"] for r in a.get("/api/policies").json()["rules"])
    assert conn.execute("SELECT revoked_at FROM sessions WHERE id=?", (ids["session"],)).fetchone()[0] is None
    assert conn.execute("SELECT status FROM integration_accounts WHERE id=?", (ids["account"],)).fetchone()[0] == "connected"
    assert conn.execute("SELECT read_at FROM notifications WHERE id=?", (ids["notification"],)).fetchone()[0] is None
    assert conn.execute("SELECT text FROM transcript_versions WHERE capture_id=? ORDER BY version DESC",
                        (ids["capture"],)).fetchone()[0].find(MARK) >= 0
    assert conn.execute("SELECT display_name FROM users WHERE id=?", (OWNER,)).fetchone()[0] == "AJ"


def test_shared_documents_are_readable_but_not_writable(client, world, conn):
    b = _make_user(client, "member", "sam")
    doc = world["doc"]
    assert b.get(f"/api/documents/{doc}").status_code == 404
    assert not [h for h in b.get(f"/api/search?q={MARK}").json()]
    client.post(f"/api/documents/{doc}/visibility", {"visibility": "org"})
    assert b.get(f"/api/documents/{doc}").status_code == 200
    hits = b.get(f"/api/search?q={MARK}").json()
    assert hits and all(not h["own"] for h in hits)
    assert b.delete(f"/api/documents/{doc}").status_code == 404
    assert b.post(f"/api/documents/{doc}/visibility", {"visibility": "private"}).status_code == 404
    listed = {d["id"]: d for d in b.get("/api/documents").json()}
    assert listed[doc]["own"] is False
    # A's memories stay private even when a document is shared
    assert MARK not in b.get("/api/memories").text


def test_insert_without_owner_is_refused_by_the_database(client, conn):
    import sqlite3
    for sql in ("INSERT INTO conversations (id, created_at, updated_at) VALUES ('c_x','t','t')",
                "INSERT INTO memories (id, type, status, created_at, updated_at) VALUES ('m_x','fact','active','t','t')",
                "INSERT INTO proposals (id, status, title, created_at, updated_at) VALUES ('p_x','proposed','t','t','t')",
                "INSERT INTO notifications (id, kind, title, body, created_at) VALUES ('n_x','k','t','b','t')",
                "INSERT INTO integration_accounts (id, provider, label, created_at, updated_at) VALUES ('a_x','todoist','x','t','t')"):
        with pytest.raises(sqlite3.IntegrityError, match="user_id"):
            conn.execute(sql)


def test_connecting_the_same_provider_never_replaces_another_users_account(client, conn):
    """Regression for the V4 single-owner bug: B connecting Todoist must leave A's account and grants alone."""
    from jarvis.integrations import accounts as A
    conn.execute("BEGIN")
    a_id = A.upsert_account(conn, OWNER, "todoist", "A", "tok-a-123456789012345", "api_token", external_account_id="ua")
    A.set_grants(conn, a_id, "todoist_project", "write", [("p1", "Inbox")], default_target="p1")
    conn.execute("COMMIT")
    b = _make_user(client, "member", "bob")
    b_uid = conn.execute("SELECT id FROM users WHERE username='bob'").fetchone()[0]
    conn.execute("BEGIN")
    b_id = A.upsert_account(conn, b_uid, "todoist", "B", "tok-b-123456789012345", "api_token", external_account_id="ub")
    conn.execute("COMMIT")
    assert b_id != a_id
    assert conn.execute("SELECT status FROM integration_accounts WHERE id=?", (a_id,)).fetchone()[0] == "connected"
    assert A.grants(conn, a_id, "todoist_project", "write")
    # and B can't take over A's external account by connecting the same one
    conn.execute("BEGIN")
    with pytest.raises(A.AccountInUse):
        A.upsert_account(conn, b_uid, "todoist", "B2", "tok-c-123456789012345", "api_token", external_account_id="ua")
    conn.execute("ROLLBACK")
    _ = b, LOOPBACK, PASSWORD


def test_executor_refuses_an_account_that_isnt_the_plan_owners(client, fakes, conn):
    """Even a hand-forged action pointing at someone else's account is refused at execution time."""
    from conftest import run_worker
    from jarvis.integrations import accounts as A
    b = _make_user(client, "member", "eve")
    b_uid = conn.execute("SELECT id FROM users WHERE username='eve'").fetchone()[0]
    conn.execute("BEGIN")
    eve_acct = A.upsert_account(conn, b_uid, "todoist", "Eve", "tok-e-123456789012345", "api_token", external_account_id="ue")
    A.set_grants(conn, eve_acct, "todoist_project", "write", [("proj-jarvis", "Jarvis")], default_target="proj-jarvis")
    conn.execute("COMMIT")
    A.set_override("todoist", fakes["todoist"], uid=b_uid)
    r = b.post("/api/commands", {"text": "add a task to file expenses tomorrow", "captured_at": "2026-09-28T20:00:00Z",
                                 "tz": "America/Chicago"}).json()
    pid = r["proposal"]["id"]
    # forge: point Eve's queued action at AJ's account
    conn.execute("UPDATE proposed_actions SET account_id=? WHERE proposal_version_id IN "
                 "(SELECT id FROM proposal_versions WHERE proposal_id=?)", (fakes["todoist_account"], pid))
    v = b.get(f"/api/proposals/{pid}").json()
    assert b.post(f"/api/proposals/{pid}/approve", {"version": v["version"], "hash": v["hash"],
                                                    "selected_items": [0]}).status_code == 200
    run_worker(conn)
    after = b.get(f"/api/proposals/{pid}").json()["items"][0]
    assert after["state"] == "failed" and "account" in (after["last_error"] or "")
    assert not [t for t in fakes["todoist"].list_tasks() if "expenses" in t.content]


def test_rules_only_auto_approve_their_authors_proposals(client, fakes, conn):
    from jarvis.integrations import accounts as A
    client.post("/api/policies", {"action_type": "task.create", "label": "AJ's tasks are fine"})
    b = _make_user(client, "member", "max")
    b_uid = conn.execute("SELECT id FROM users WHERE username='max'").fetchone()[0]
    conn.execute("BEGIN")
    acct = A.upsert_account(conn, b_uid, "todoist", "Max", "tok-m-123456789012345", "api_token", external_account_id="um")
    A.set_grants(conn, acct, "todoist_project", "write", [("proj-jarvis", "Jarvis")], default_target="proj-jarvis")
    conn.execute("COMMIT")
    A.set_override("todoist", fakes["todoist"], uid=b_uid)
    r = b.post("/api/commands", {"text": "add a task to renew passport tomorrow", "captured_at": "2026-09-28T20:00:00Z",
                                 "tz": "America/Chicago"}).json()
    assert r["proposal"]["status"] == "proposed"          # AJ's rule did not approve Max's plan
