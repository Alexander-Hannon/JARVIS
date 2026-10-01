"""Service-level behavior around adapters: ICS refresh/classification, Drive allowlist, Todoist reconciliation,
Google OAuth state validation, calendar ownership. All provider I/O is faked (no network)."""
from __future__ import annotations

import pytest
from conftest import CHICAGO_AFTERNOON, freeze

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:a1@canvas
DTSTAMP:20260901T000000Z
DTSTART;VALUE=DATE:20260930
DTEND;VALUE=DATE:20261001
SUMMARY:MKT 601 Case 2 due
END:VEVENT
BEGIN:VEVENT
UID:a2@canvas
DTSTAMP:20260901T000000Z
DTSTART:20261001T045900Z
DTEND:20261001T045900Z
SUMMARY:IS 640 HW4 grading window closes
END:VEVENT
BEGIN:VEVENT
UID:a3@canvas
DTSTAMP:20260901T000000Z
DTSTART;VALUE=DATE:20261002
SUMMARY:Mystery item
END:VEVENT
END:VCALENDAR
"""


def test_ics_refresh_classifies_with_confirmed_mappings_and_hides_url(client, conn, fakes):
    freeze(CHICAGO_AFTERNOON)
    from jarvis.integrations import service
    conn.execute("UPDATE course_mappings SET confirmed=1 WHERE pattern IN ('MKT 601','IS 640')")
    with pytest.raises(service.SetupError):
        service.connect_ics(conn, "http://insecure.example/x.ics")
    from jarvis.integrations import accounts as A
    conn.execute("BEGIN")
    A.upsert_account(conn, "ics", "Deadline feed", "https://feeds.example.com/private_TOKEN.ics", "ics_url",
                     external_account_id="ics-feed")
    conn.execute("COMMIT")
    res = service.ics_refresh(conn, fetcher=lambda url: ICS)
    assert res["added"] == 3
    d = {x["summary"]: x for x in client.get("/api/deadlines").json()}
    assert d["MKT 601 Case 2 due"]["role"] == "own" and d["MKT 601 Case 2 due"]["all_day"] == 1
    assert d["IS 640 HW4 grading window closes"]["role"] == "gta"
    assert d["IS 640 HW4 grading window closes"]["local_date"] == "2026-09-30"     # 04:59Z = 23:59 CDT Sep 30
    assert d["Mystery item"]["role"] == "unknown"
    assert "private_TOKEN" not in str(client.get("/api/integrations").json())
    today = client.get("/api/today").json()
    assert today["sources"]["deadlines"]["status"] == "ok"
    # failure keeps last good data and is visible
    with pytest.raises(service.SetupError):
        service.ics_refresh(conn, fetcher=lambda url: (_ for _ in ()).throw(TimeoutError()))
    assert client.get("/api/today").json()["sources"]["deadlines"]["status"] in ("stale", "unavailable")
    assert len(client.get("/api/deadlines").json()) == 3


def test_todoist_sync_reconciles_external_changes(client, fakes, conn):
    freeze(CHICAGO_AFTERNOON)
    from conftest import run_worker

    from jarvis.integrations import service
    r = client.post("/api/commands", {"text": "buy pens tomorrow and buy folders tomorrow",
                                      "captured_at": "2026-09-28T20:00:00Z"}).json()
    p = r["proposal"]
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0, 1]})
    run_worker(conn)
    ids = [i["external_id"] for i in client.get(f"/api/proposals/{p['id']}").json()["items"]]
    fakes["todoist"].external_complete(ids[0])
    fakes["todoist"].external_delete(ids[1])
    out = service.todoist_sync(conn)
    assert out["reconciled"] == 2
    st = {r["external_id"]: r["status"] for r in conn.execute("SELECT external_id, status FROM task_links")}
    assert st == {ids[0]: "completed", ids[1]: "deleted"}


def test_google_oauth_state_validation(client, conn, monkeypatch):
    monkeypatch.setenv("JARVIS_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("JARVIS_GOOGLE_CLIENT_SECRET", "test-secret")
    from jarvis.config import reset_settings_cache
    reset_settings_cache()
    from urllib.parse import parse_qs, urlsplit
    url = client.post("/api/integrations/google/start", {"purpose": "calendar"}).json()["auth_url"]
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/api/integrations/google/callback")
    assert "calendar.events.owned" in q["scope"][0] and "https://www.googleapis.com/auth/calendar " not in q["scope"][0] + " "
    # forged / unknown state is refused
    r = client.c.get("/api/integrations/google/callback?state=forged&code=abc", follow_redirects=False)
    assert r.status_code == 303 and "connect_error=bad_state" in r.headers["location"]


def test_google_callback_works_without_strict_session_cookie(client, conn, monkeypatch):
    """Regression: Google's cross-site redirect doesn't carry the SameSite=Strict session cookie.
    The callback must succeed with state + Lax binding cookie, and refuse other browsers."""
    monkeypatch.setenv("JARVIS_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("JARVIS_GOOGLE_CLIENT_SECRET", "test-secret")
    from urllib.parse import parse_qs, urlsplit

    from fastapi.testclient import TestClient

    from jarvis.config import reset_settings_cache
    from jarvis.integrations import google_oauth
    reset_settings_cache()
    monkeypatch.setattr(google_oauth, "exchange_code", lambda *a, **k: {"access_token": "at", "refresh_token": "refresh-PLAINTEXT-4f9c2a",
                                                                        "expires_at": 9e9, "scope": "openid email"})
    monkeypatch.setattr(google_oauth, "fetch_userinfo", lambda *a, **k: {"email": "aj@uah.example"})
    start = client.post("/api/integrations/google/start", {"purpose": "calendar"})
    state = parse_qs(urlsplit(start.json()["auth_url"]).query)["state"][0]
    binding = start.cookies.get("jarvis_oauth_binding")
    assert binding
    # a different browser (no binding cookie, no session) is refused
    other = TestClient(client.c.app)
    r = other.get(f"/api/integrations/google/callback?state={state}&code=abc", follow_redirects=False)
    assert "connect_error=wrong_browser" in r.headers["location"]
    # same browser, session cookie withheld (as with a cross-site redirect) — succeeds
    same = TestClient(client.c.app, cookies={"jarvis_oauth_binding": binding})
    r = same.get(f"/api/integrations/google/callback?state={state}&code=abc", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connections?connected=google"
    acct = conn.execute("SELECT label, config, secret_ciphertext FROM integration_accounts WHERE provider='google'").fetchone()
    assert acct["label"] == "aj@uah.example" and b"refresh-PLAINTEXT-4f9c2a" not in acct["secret_ciphertext"]
    # replaying the same state is refused
    r = same.get(f"/api/integrations/google/callback?state={state}&code=abc", follow_redirects=False)
    assert "connect_error=bad_state" in r.headers["location"]


def test_calendar_write_target_must_be_owned(client, fakes, conn):
    fakes["calendar"].add_calendar("shared@group", "Department", access_role="writer")
    r = client.post("/api/integrations/google/calendars", {"read": ["primary", "shared@group"], "write_target": "shared@group"})
    assert r.status_code == 400 and "own" in r.text
    ok = client.post("/api/integrations/google/calendars", {"read": ["primary", "shared@group"], "write_target": "primary"})
    assert ok.status_code == 200
    cals = {c["id"]: c for c in client.get("/api/integrations/google/calendars").json()}
    assert cals["primary"]["write_target"] and cals["shared@group"]["selected_read"] and not cals["shared@group"]["write_target"]


class FakeDrive:
    name = "google_drive"
    simulated = True

    def __init__(self):
        self.files = {"fold1": {"id": "fold1", "name": "GTA", "mimeType": "application/vnd.google-apps.folder", "parents": []},
                      "doc1": {"id": "doc1", "name": "Rubric", "mimeType": "application/vnd.google-apps.document",
                               "parents": ["fold1"], "modifiedTime": "2026-09-01T00:00:00Z"},
                      "doc2": {"id": "doc2", "name": "Taxes", "mimeType": "application/pdf", "parents": ["other"]}}

    def get_file(self, fid):
        return self.files[fid]

    def download(self, fid, mime, max_bytes):
        return (b"Rubric: clarity 30%, accuracy 70%", "text/plain")


def test_drive_selected_roots_only(client, conn):
    from jarvis.integrations import accounts as A
    from jarvis.integrations import service
    A.set_override("google_drive", FakeDrive())
    conn.execute("BEGIN")
    A.upsert_account(conn, "google", "aj@example.test", '{"access_token":"x"}', "oauth_tokens", external_account_id="aj@example.test",
                     config={"purposes": ["drive"]})
    conn.execute("COMMIT")
    client.post("/api/integrations/drive/roots", {"ids": ["fold1"]})
    ok = service.drive_ingest(conn, "doc1")
    assert ok["status"] in ("processed", "partial") and ok["title"].startswith("Rubric")
    with pytest.raises(service.SetupError):
        service.drive_ingest(conn, "doc2")
    hits = client.get("/api/search?q=clarity").json()
    assert hits and hits[0]["title"].startswith("Rubric")


def test_choosing_todoist_project_unblocks_open_proposal(client, fakes, conn):
    """Reported: checkbox couldn't be ticked because no destination project was chosen. Picking one fixes open plans."""
    freeze(CHICAGO_AFTERNOON)
    conn.execute("DELETE FROM scope_grants WHERE resource_type='todoist_project'")
    r = client.post("/api/commands", {"text": "go grocery shopping today and then remind me to grade MSC 550 tomorrow",
                                      "captured_at": "2026-09-28T20:00:00Z"}).json()
    p = r["proposal"]
    assert all(i["missing_fields"] for i in p["items"])
    assert client.post("/api/integrations/todoist/target", {"project_id": "proj-jarvis"}).status_code == 200
    v = client.get(f"/api/proposals/{p['id']}").json()
    assert v["version"] == 2 and all(not i["missing_fields"] for i in v["items"])
    assert all(i["args"]["project_id"] == "proj-jarvis" for i in v["items"])
    ok = client.post(f"/api/proposals/{p['id']}/approve", {"version": 2, "hash": v["hash"], "selected_items": [0, 1]})
    assert ok.status_code == 200


def test_gzip_feed_and_url_never_logged(client, conn, caplog):
    """Regression (AJ's worker log): Canvas serves gzip; body was decoded twice. And httpx logged the private URL."""
    import gzip
    import logging

    import httpx
    import respx

    from jarvis.integrations import accounts as A
    from jarvis.integrations import service
    from jarvis.logsafe import configure
    configure()
    freeze(CHICAGO_AFTERNOON)
    url = "https://uah.instructure.com/feeds/calendars/user_SECRETTOKEN123.ics"
    conn.execute("BEGIN")
    A.upsert_account(conn, "ics", "Deadline feed", url, "ics_url", external_account_id="ics-feed")
    conn.execute("COMMIT")
    caplog.set_level(logging.INFO)
    with respx.mock() as m:
        m.get(url).mock(return_value=httpx.Response(200, content=gzip.compress(ICS), headers={"Content-Encoding": "gzip"}))
        from jarvis.security import netguard
        real = netguard.validate_url
        netguard.validate_url = lambda u, allow=None, resolver=None: u     # skip DNS in tests
        try:
            res = service.ics_refresh(conn)
        finally:
            netguard.validate_url = real
    assert res["items"] == 3
    assert "SECRETTOKEN123" not in caplog.text
    from jarvis.logsafe import RedactURLs
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "fetched %s", (url,), None)
    RedactURLs().filter(rec)
    assert "SECRETTOKEN123" not in rec.getMessage() and "[redacted]" in rec.getMessage()
