"""Negative tests for consequential boundaries (auth, CSRF, origin, host, secrets, SSRF, file sandbox)."""
from __future__ import annotations

import os
import tarfile

import pytest
from fastapi.testclient import TestClient


def test_all_data_endpoints_require_auth(app):
    c = TestClient(app)
    for m, path in [("get", "/api/today"), ("post", "/api/commands"), ("get", "/api/proposals"), ("get", "/api/memories"),
                    ("post", "/api/captures/audio"), ("get", "/api/doctor"), ("post", "/api/backups"),
                    ("get", "/api/localops/roots"), ("post", "/api/control/pause"), ("get", "/api/integrations")]:
        r = getattr(c, m)(path)
        assert r.status_code == 401, (path, r.status_code)


def test_setup_only_once_and_only_loopback(app):
    c = TestClient(app, client=("203.0.113.9", 5555))
    assert c.get("/api/auth/status").json()["setup_allowed"] is False
    assert c.post("/api/auth/setup", json={"password": "x" * 12}).status_code == 403
    local = TestClient(app)
    assert local.post("/api/auth/setup", json={"password": "short"}).status_code == 400
    assert local.post("/api/auth/setup", json={"password": "long enough pw"}).status_code == 200
    assert local.post("/api/auth/setup", json={"password": "another long pw"}).status_code in (403, 409)


def test_login_and_wrong_password(client):
    c = TestClient(client.c.app)
    assert c.post("/api/auth/login", json={"password": "wrong password!"}).status_code == 401
    assert c.post("/api/auth/login", json={"password": "correct horse battery"}).status_code == 200


def test_csrf_required_for_cookie_writes(client):
    r = client.c.post("/api/commands", json={"text": "buy milk"})
    assert r.status_code == 403
    r = client.c.post("/api/commands", json={"text": "buy milk"}, headers={"X-CSRF-Token": "nope"})
    assert r.status_code == 403


def test_cross_origin_and_foreign_host_refused(client):
    r = client.c.post("/api/control/pause", json={}, headers={"X-CSRF-Token": client.csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.c.get("/api/auth/status", headers={"Host": "attacker.example"})
    assert r.status_code == 421                         # DNS-rebinding defense


def test_pairing_code_single_use(client):
    code = client.post("/api/auth/pairing-code").json()["code"]
    c = TestClient(client.c.app)
    assert c.post("/api/auth/pair", json={"code": code, "label": "phone"}).status_code == 200
    c2 = TestClient(client.c.app)
    assert c2.post("/api/auth/pair", json={"code": code, "label": "phone2"}).status_code == 401


def test_secrets_never_leak_to_api_logs_or_backups(client, conn, env, caplog):
    from jarvis.integrations import accounts as A
    token = "tdst_SUPERSECRET_1234567890abcdef"
    feed = "https://canvas.example.edu/feeds/calendars/user_PRIVATEFEEDTOKEN.ics"
    conn.execute("BEGIN")
    A.upsert_account(conn, "todoist", "me", token, "api_token", external_account_id="u9")
    A.upsert_account(conn, "ics", "Deadline feed", feed, "ics_url", external_account_id="ics-feed")
    conn.execute("COMMIT")
    blob = ""
    for path in ["/api/integrations", "/api/doctor", "/api/settings", "/api/activity", "/api/budget"]:
        blob += client.get(path).text
    assert token not in blob and "PRIVATEFEEDTOKEN" not in blob
    raw = conn.execute("SELECT secret_ciphertext FROM integration_accounts WHERE provider='todoist'").fetchone()[0]
    assert token.encode() not in raw                     # encrypted at rest
    b = client.post("/api/backups").json()
    with tarfile.open(b["path"]) as tar:
        names = tar.getnames()
        assert "secret.key" not in " ".join(names)
        db = tar.extractfile("jarvis.db").read()
        assert token.encode() not in db and b"PRIVATEFEEDTOKEN" not in db
    assert token not in caplog.text and "PRIVATEFEEDTOKEN" not in caplog.text
    # key file permissions
    key = env / "data" / "secret.key"
    assert oct(os.stat(key).st_mode & 0o777) == "0o600"


def test_missing_encryption_key_fails_loudly(client, conn, env):
    from jarvis.integrations import accounts as A
    from jarvis.security import secrets
    conn.execute("BEGIN")
    aid = A.upsert_account(conn, "github", "gh", "ghp_x", "pat", external_account_id="github")
    conn.execute("COMMIT")
    (env / "data" / "secret.key").rename(env / "data" / "moved.key")
    with pytest.raises(secrets.SecretKeyUnavailable):
        A.read_secret(A.get_account_by_id(conn, aid))


@pytest.mark.parametrize("url", ["http://example.com/feed.ics", "https://127.0.0.1/feed.ics", "https://169.254.169.254/latest",
                                 "https://user:pw@example.com/x.ics", "file:///etc/passwd", "https://10.0.0.5/cal.ics"])
def test_ics_url_ssrf_guard(url):
    from jarvis.security.netguard import UnsafeURL, validate_url

    def resolver(host, port, proto=0):
        ip = host if host.replace(".", "").isdigit() else "93.184.216.34"
        return [(2, 1, 6, "", (ip, port))]
    with pytest.raises(UnsafeURL):
        validate_url(url, resolver=resolver)


def test_ics_url_rebinding_to_private_ip_refused():
    from jarvis.security.netguard import UnsafeURL, validate_url
    with pytest.raises(UnsafeURL):
        validate_url("https://feeds.example.com/x.ics", resolver=lambda h, p, proto=0: [(2, 1, 6, "", ("192.168.1.10", p))])
    ok = validate_url("webcal://feeds.example.com/x.ics", resolver=lambda h, p, proto=0: [(2, 1, 6, "", ("93.184.216.34", p))])
    assert ok.startswith("https://")


def test_revise_cannot_change_destination_or_account(client, fakes):
    r = client.post("/api/commands", {"text": "buy stamps tomorrow", "captured_at": "2026-09-28T20:00:00Z"}).json()
    pid = r["proposal"]["id"]
    bad = client.post(f"/api/proposals/{pid}/revise", {"items": [{"index": 0, "args": {"project_id": "someone-elses"}}]})
    assert bad.status_code == 400


def test_unsupported_action_types_cannot_be_proposed(conn):
    from jarvis.actions import proposals as P
    with pytest.raises(P.ProposalError):
        conn.execute("BEGIN")
        try:
            P.create(conn, "x", [P.ActionSpec("email.send", {"to": "a@b"}, "UTC", "send mail")], "x", conversation_id=None)
        finally:
            conn.execute("ROLLBACK")


def test_executor_refuses_write_without_grant(client, fakes, conn):
    from conftest import run_worker
    r = client.post("/api/commands", {"text": "buy stamps tomorrow", "captured_at": "2026-09-28T20:00:00Z"}).json()
    p = r["proposal"]
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    conn.execute("DELETE FROM scope_grants WHERE resource_type='todoist_project'")   # AJ revoked the project grant
    run_worker(conn)
    v = client.get(f"/api/proposals/{p['id']}").json()
    assert v["items"][0]["state"] == "failed" and "No write permission" in v["items"][0]["last_error"]
    assert fakes["todoist"].list_tasks() == []


def test_frontend_bundle_has_no_secrets():
    from pathlib import Path
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dist.exists():
        pytest.skip("frontend not built")
    text = "".join(p.read_text(errors="ignore") for p in dist.rglob("*") if p.is_file() and p.suffix in (".js", ".html", ".css"))
    import re
    for pat in (r"sk-ant-[A-Za-z0-9_\-]{20,}", r"GOCSPX-[A-Za-z0-9_\-]{10,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}",
                r"JARVIS_SECRET_KEY\s*=", r"VAPID_PRIVATE", r"client_secret\s*[:=]\s*[\"'][^\"']{8,}"):
        assert not re.search(pat, text), pat
