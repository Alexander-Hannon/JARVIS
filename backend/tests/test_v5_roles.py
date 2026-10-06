"""V5 M1a roles: the permission matrix per role, co-approval for org-impacting actions, owner-only role grants, and
audit visibility (admins/auditors see who did what, never other people's content)."""
from __future__ import annotations

import pytest
from conftest import OWNER, Api, run_worker
from fastapi.testclient import TestClient

ROLES = ["owner", "admin", "approver", "member", "auditor"]
IP = {"admin": "203.0.113.71", "approver": "203.0.113.72", "member": "203.0.113.73", "auditor": "203.0.113.74",
      "admin2": "203.0.113.75"}


def make(owner: Api, role: str, username: str | None = None) -> tuple[Api, str]:
    username = username or role
    inv = owner.post("/api/users", {"username": username, "display_name": username.title(), "role": role})
    assert inv.status_code == 200, inv.text
    c = TestClient(owner.c.app, client=(IP.get(username, "203.0.113.99"), 41000))
    r = c.post("/api/auth/invite/accept", json={"token": inv.json()["invite_url"].rsplit("/", 1)[-1],
                                                "password": f"{username} password 123"})
    assert r.status_code == 200, r.text
    return Api(c, r.json()["csrf_token"]), inv.json()["user"]["id"]


@pytest.fixture
def team(client, monkeypatch):
    from jarvis.config import reset_settings_cache
    monkeypatch.setenv("JARVIS_EDITION", "enterprise")       # a company install (co-approval applies)
    reset_settings_cache()
    out = {"owner": client}
    for role in ROLES[1:]:
        out[role], _ = make(client, role)
    return out


MATRIX = [
    # (method, path, body, roles allowed, status when allowed)
    ("POST", "/api/commands", {"text": "what's the weather", "captured_at": "2026-09-28T20:00:00Z"},
     {"owner", "admin", "approver", "member"}, 200),
    ("GET", "/api/users", None, {"owner", "admin"}, 200),
    ("POST", "/api/mcp", {"name": "!", "transport": "http"}, {"owner", "admin"}, 400),
    ("PATCH", "/api/budget", {"effort": "low"}, {"owner", "admin"}, 200),
    ("GET", "/api/backups", None, {"owner", "admin"}, 200),
    ("GET", "/api/doctor", None, {"owner", "admin"}, 200),
    ("POST", "/api/backups/bk_none/restore", {"confirm": True}, {"owner"}, 400),
    ("POST", "/api/skills/install", {"folder": "/nonexistent"}, {"owner", "admin"}, 404),
    ("POST", "/api/channels/telegram", {"token": "nope"}, {"owner", "admin"}, 400),
    ("GET", "/api/auth/me", None, set(ROLES), 200),
    ("GET", "/api/activity", None, set(ROLES), 200),
    ("GET", "/api/control", None, set(ROLES), 200),
]


@pytest.mark.parametrize("role", ROLES)
def test_permission_matrix(team, role):
    api = team[role]
    for method, path, body, allowed, ok_status in MATRIX:
        r = {"GET": api.get, "POST": api.post, "PATCH": api.patch}[method](path, *([body] if body is not None else []))
        want = ok_status if role in allowed else 403
        assert r.status_code == want, f"{role} {method} {path}: {r.status_code} {r.text[:200]}"


def test_pause_is_an_admin_switch(team):
    assert team["member"].post("/api/control/pause", {}).status_code == 403
    assert team["admin"].post("/api/control/pause", {"reason": "drill"}).status_code == 200
    assert team["member"].get("/api/control").json()["actions_paused"] is True
    assert team["owner"].post("/api/control/resume").status_code == 200


def test_only_owners_grant_owner_or_admin_and_the_last_owner_stays(team, conn):
    admin, member = team["admin"], team["member"]
    mid = conn.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
    assert admin.patch(f"/api/users/{mid}", {"role": "admin"}).status_code == 403
    assert admin.post("/api/users", {"username": "ann", "display_name": "Ann", "role": "admin"}).status_code == 403
    assert admin.patch(f"/api/users/{mid}", {"role": "approver"}).status_code == 200
    assert admin.patch(f"/api/users/{OWNER}", {"display_name": "Pwned"}).status_code == 403
    assert admin.patch(f"/api/users/{OWNER}", {"status": "disabled"}).status_code == 403
    assert team["owner"].patch(f"/api/users/{OWNER}", {"role": "member"}).status_code == 400     # can't change self
    aid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    assert team["owner"].patch(f"/api/users/{aid}", {"role": "owner"}).status_code == 200
    assert team["owner"].patch(f"/api/users/{aid}", {"role": "member"}).status_code == 200      # another owner remains
    assert member.get("/api/users").status_code == 403


def test_disabling_a_user_ends_sessions_and_pauses_their_work(team, conn):
    member = team["member"]
    member.post("/api/missions", {"name": "m", "goal": "g", "schedule": {"kind": "daily", "at": "07:00"}, "families": []})
    mid = conn.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
    assert team["admin"].patch(f"/api/users/{mid}", {"status": "disabled"}).status_code == 200
    assert member.get("/api/today").status_code == 401
    assert conn.execute("SELECT COUNT(*) FROM missions WHERE user_id=? AND enabled=1", (mid,)).fetchone()[0] == 0
    c = TestClient(member.c.app, client=("203.0.113.73", 41001))
    assert c.post("/api/auth/login", json={"username": "member", "password": "member password 123"}).status_code == 401


# ---------------------------------------------------------------- co-approval
def _skill_proposal(conn, uid: str, name: str = "standup-notes") -> str:
    from jarvis.actions import proposals as P
    spec = P.ActionSpec("skill.install", {"name": name, "description": "Draft standup notes",
                                          "body": "1. Read my calendar\n2. Summarize"}, "UTC",
                        f"Install skill {name} for everyone")
    conn.execute("BEGIN")
    pid = P.create(conn, uid, f"Install {name}", [spec], "skill", conversation_id=None)
    conn.execute("COMMIT")
    return pid


def test_org_wide_skill_needs_a_second_permitted_person(team, conn, tmp_path):
    member, admin, approver = team["member"], team["admin"], team["approver"]
    m_uid = conn.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
    pid = _skill_proposal(conn, m_uid)
    v = member.get(f"/api/proposals/{pid}").json()
    r = member.post(f"/api/proposals/{pid}/approve", {"version": v["version"], "hash": v["hash"], "selected_items": [0]})
    assert r.status_code == 200 and r.json()["awaiting_co_approval"] is True
    assert member.get(f"/api/proposals/{pid}").json()["status"] == "awaiting_co_approval"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='execute_action'").fetchone()[0] == 0
    # visible to people who can co-approve (admins), not to the approver role (no org.integrations) or the requester
    assert [p["id"] for p in admin.get("/api/approvals/co").json()] == [pid]
    assert approver.get("/api/approvals/co").json() == []
    assert member.get("/api/approvals/co").json() == []
    assert approver.post(f"/api/approvals/co/{pid}", {"version": v["version"], "hash": v["hash"]}).status_code == 404
    assert member.post(f"/api/approvals/co/{pid}", {"version": v["version"], "hash": v["hash"]}).status_code == 404
    item = admin.get("/api/approvals/co").json()[0]
    assert item["requested_username"] == "member" and "Summarize" in item["items"][0]["args"]["body"]
    assert admin.post(f"/api/approvals/co/{pid}", {"version": v["version"], "hash": v["hash"]}).json()["status"] == "queued"
    run_worker(conn)
    assert member.get(f"/api/proposals/{pid}").json()["status"] == "verified"
    assert any(s["name"] == "standup-notes" for s in member.get("/api/skills").json())


def test_co_approver_who_lost_the_role_before_execution_voids_it(team, conn):
    member, admin = team["member"], team["admin"]
    m_uid = conn.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
    a_uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    pid = _skill_proposal(conn, m_uid, "risky")
    v = member.get(f"/api/proposals/{pid}").json()
    member.post(f"/api/proposals/{pid}/approve", {"version": v["version"], "hash": v["hash"], "selected_items": [0]})
    admin.post(f"/api/approvals/co/{pid}", {"version": v["version"], "hash": v["hash"]})
    team["owner"].patch(f"/api/users/{a_uid}", {"role": "member"})          # demoted before the worker ran
    run_worker(conn)
    item = member.get(f"/api/proposals/{pid}").json()["items"][0]
    assert item["state"] == "cancelled" and "co-approver" in item["last_error"]
    assert not any(s["name"] == "risky" for s in member.get("/api/skills").json())


def test_an_admins_own_org_change_needs_another_admin(team, conn):
    admin = team["admin"]
    a_uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    pid = _skill_proposal(conn, a_uid, "self-serve")
    v = admin.get(f"/api/proposals/{pid}").json()
    assert admin.post(f"/api/proposals/{pid}/approve", {"version": v["version"], "hash": v["hash"],
                                                        "selected_items": [0]}).json()["awaiting_co_approval"]
    assert admin.post(f"/api/approvals/co/{pid}", {"version": v["version"], "hash": v["hash"]}).status_code == 404
    assert [p["id"] for p in team["owner"].get("/api/approvals/co").json()] == [pid]


def test_declined_co_approval_rejects_the_plan(team, conn):
    member, admin = team["member"], team["admin"]
    m_uid = conn.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
    pid = _skill_proposal(conn, m_uid, "nope")
    v = member.get(f"/api/proposals/{pid}").json()
    member.post(f"/api/proposals/{pid}/approve", {"version": v["version"], "hash": v["hash"], "selected_items": [0]})
    assert admin.post(f"/api/approvals/co/{pid}", {"version": v["version"], "hash": v["hash"],
                                                   "decision": "decline"}).json()["status"] == "rejected"
    assert member.get(f"/api/proposals/{pid}").json()["status"] == "rejected"


def test_co_approved_types_can_never_be_automated(client):
    from jarvis.actions import registry as R
    with pytest.raises(ValueError):
        R.register(R.ActionType("x.org", "x", "m:f", R.Tier.T1, "none", "x", co_approver_perm="org.integrations"))
    assert client.post("/api/policies", {"action_type": "skill.install", "label": "auto"}).status_code == 400


# ---------------------------------------------------------------- audit visibility
def test_admins_and_auditors_see_who_did_what_but_not_what_it_said(team):
    member, admin, auditor = team["member"], team["admin"], team["auditor"]
    member.post("/api/memories", {"content": "SECRETMARK my therapist is Dr. Q"})
    for viewer in (admin, auditor):
        org = viewer.get("/api/activity?scope=org").json()
        mine = [e for e in org if e.get("username") == "member" and e["kind"] == "memory.created"]
        assert mine and "SECRETMARK" not in str(org)
        assert mine[0]["message"] == "memory: created"
    own = member.get("/api/activity?scope=org").json()          # members can't widen the scope
    assert all(e.get("username") in (None, "member") for e in own)
    assert any("SECRETMARK" in (e.get("message") or "") for e in own)  # their own summaries stay readable
    assert auditor.post("/api/commands", {"text": "hi"}).status_code == 403


def test_personal_install_keeps_single_person_approval(client, conn):
    """A personal install has nobody to co-approve: the owner's explicit approval is enough (V4 behavior)."""
    pid = _skill_proposal(conn, OWNER, "solo")
    v = client.get(f"/api/proposals/{pid}").json()
    r = client.post(f"/api/proposals/{pid}/approve", {"version": v["version"], "hash": v["hash"], "selected_items": [0]})
    assert not r.json().get("awaiting_co_approval")
    run_worker(conn)
    assert any(s["name"] == "solo" for s in client.get("/api/skills").json())
