import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from jarvis.integrations import google_oauth as go
from jarvis.integrations.base import ProviderError
from jarvis.integrations.google_calendar import (
    GoogleCalendarAdapter,
    event_id_from_op_key,
)
from jarvis.integrations.google_drive import GoogleDriveAdapter

CAL = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
NOW = 1_800_000_000.0


def make_creds(client, *, expires_at=NOW + 3600, updater=None, clock=lambda: NOW):
    return go.GoogleCredentials({"access_token": "at-1", "refresh_token": "rt-1", "expires_at": expires_at,
                                 "scope": " ".join(go.CALENDAR_SCOPES)}, "cid", "csecret",
                                token_updater=updater, http_client=client, clock=clock)


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c


# ------------------------------------------------------------------ OAuth

def test_auth_url_has_pkce_state_offline_consent():
    verifier, challenge = go.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128 and challenge == go.pkce_challenge(verifier)
    url = go.build_auth_url("cid", "http://localhost:8000/oauth/google/callback", go.CALENDAR_SCOPES,
                            "s" * 32, challenge)
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == go.AUTH_URL
    assert q["code_challenge"] == challenge and q["code_challenge_method"] == "S256"
    assert q["state"] == "s" * 32 and q["response_type"] == "code"
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["include_granted_scopes"] == "true"
    assert q["scope"].split() == go.CALENDAR_SCOPES
    with pytest.raises(ProviderError):
        go.build_auth_url("cid", "http://x", go.CALENDAR_SCOPES, "short", challenge)


def test_scope_choices_are_least_privilege():
    assert "https://www.googleapis.com/auth/calendar" not in go.CALENDAR_SCOPES
    assert "https://www.googleapis.com/auth/calendar.events" not in go.CALENDAR_SCOPES
    assert all("drive" not in s or s.endswith("readonly") for s in go.DRIVE_SCOPES)


@respx.mock
def test_exchange_code_sends_verifier_and_computes_expiry(client):
    route = respx.post(go.TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": "openid email",
        "token_type": "Bearer", "id_token": "idt"}))
    tok = go.exchange_code("cid", "sec", "http://cb", "code-1", "verifier-1", client=client, now=NOW)
    assert tok == {"access_token": "at", "refresh_token": "rt", "expires_at": NOW + 3599, "scope": "openid email",
                   "token_type": "Bearer", "id_token": "idt"}
    form = parse_qs(route.calls[0].request.content.decode())
    assert form["code_verifier"] == ["verifier-1"] and form["grant_type"] == ["authorization_code"]
    assert form["redirect_uri"] == ["http://cb"]


@respx.mock
def test_refresh_invalid_grant_means_reconnect(client):
    respx.post(go.TOKEN_URL).mock(return_value=httpx.Response(400, json={
        "error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    with pytest.raises(ProviderError) as e:
        go.refresh("cid", "sec", "rt", client=client)
    assert e.value.kind == "auth" and e.value.detail["reconnect_required"] is True


@respx.mock
def test_credentials_auto_refresh_calls_updater_and_keeps_refresh_token(client):
    saved = []
    route = respx.post(go.TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "at-2", "expires_in": 3600, "scope": "openid", "token_type": "Bearer"}))
    api = respx.get(f"{CAL}/users/me/calendarList").mock(return_value=httpx.Response(200, json={"items": []}))
    creds = make_creds(client, expires_at=NOW + 30, updater=saved.append)  # within 60s skew
    GoogleCalendarAdapter(creds).list_calendars()
    assert route.called and saved[0]["access_token"] == "at-2" and saved[0]["refresh_token"] == "rt-1"
    assert api.calls[0].request.headers["Authorization"] == "Bearer at-2"
    assert "at-2" not in repr(creds)


@respx.mock
def test_401_triggers_single_refresh_and_retry(client):
    respx.post(go.TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600}))
    api = respx.get(f"{CAL}/users/me/calendarList").mock(side_effect=[
        httpx.Response(401, json={"error": {"message": "expired"}}), httpx.Response(200, json={"items": []})])
    assert GoogleCalendarAdapter(make_creds(client)).list_calendars() == []
    assert api.calls[1].request.headers["Authorization"] == "Bearer at-2"


@respx.mock
def test_revoke_posts_form_token(client):
    route = respx.post(go.REVOKE_URL).mock(return_value=httpx.Response(200))
    go.revoke("rt-1", client=client)
    assert parse_qs(route.calls[0].request.content.decode()) == {"token": ["rt-1"]}


@respx.mock
def test_userinfo(client):
    respx.get(go.USERINFO_URL).mock(return_value=httpx.Response(200, json={"sub": "1", "email": "aj@gmail.com"}))
    assert go.fetch_userinfo("at", client=client)["email"] == "aj@gmail.com"


# ------------------------------------------------------------------ Calendar

def gevent(eid="ev1", **kw):
    return {"id": eid, "etag": '"e1"', "summary": "Focus", "status": "confirmed",
            "start": {"dateTime": "2026-10-01T09:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-10-01T10:00:00-04:00"}, "organizer": {"email": "aj", "self": True},
            "htmlLink": "https://calendar.google.com/x", **kw}


def test_event_id_from_op_key_is_deterministic_base32hex():
    a = event_id_from_op_key("op:123")
    assert a == event_id_from_op_key("op:123") and a != event_id_from_op_key("op:124")
    assert set(a) <= set("abcdefghijklmnopqrstuv0123456789") and 5 <= len(a) <= 1024


@respx.mock
def test_list_calendars_and_events_pagination(client):
    respx.get(f"{CAL}/users/me/calendarList").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "primary@x", "summary": "AJ", "accessRole": "owner", "primary": True, "timeZone": "America/New_York"}]}))
    ev_route = respx.get(f"{CAL}/calendars/primary%40x/events").mock(side_effect=[
        httpx.Response(200, json={"items": [gevent("a")], "nextPageToken": "p2"}),
        httpx.Response(200, json={"items": [gevent("b", recurringEventId="series", transparency="transparent",
                                                   originalStartTime={"dateTime": "2026-10-01T09:00:00Z"},
                                                   extendedProperties={"private": {"jarvis_op": "k"}}),
                                            {"id": "c", "summary": "Deadline", "start": {"date": "2026-10-02"},
                                             "end": {"date": "2026-10-03"}}]})])
    adapter = GoogleCalendarAdapter(make_creds(client))
    assert adapter.list_calendars()[0]["primary"] is True
    evs = adapter.list_events("primary@x", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z")
    assert [e.id for e in evs] == ["a", "b", "c"]
    p1 = ev_route.calls[0].request.url.params
    assert p1["singleEvents"] == "true" and p1["orderBy"] == "startTime" and p1["showDeleted"] == "false"
    assert p1["timeMin"] == "2026-10-01T00:00:00Z" and "pageToken" not in p1
    assert ev_route.calls[1].request.url.params["pageToken"] == "p2"
    b, c = evs[1], evs[2]
    assert b.recurring_event_id == "series" and not b.busy and b.private_props == {"jarvis_op": "k"}
    assert b.original_start == {"dateTime": "2026-10-01T09:00:00Z"}
    assert c.all_day and c.busy


@respx.mock
def test_freebusy_request_and_error_surface(client):
    route = respx.post(f"{CAL}/freeBusy").mock(return_value=httpx.Response(200, json={"calendars": {
        "a": {"busy": [{"start": "2026-10-01T13:00:00Z", "end": "2026-10-01T14:00:00Z"}]},
        "b": {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}}}))
    adapter = GoogleCalendarAdapter(make_creds(client))
    assert adapter.free_busy(["a"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")["a"][0]["start"] == \
        "2026-10-01T13:00:00Z"
    assert json.loads(route.calls[0].request.content)["items"] == [{"id": "a"}]
    with pytest.raises(ProviderError) as e:
        adapter.free_busy(["a", "b"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
    assert e.value.kind == "not_found"


@respx.mock
def test_insert_sends_client_id_and_send_updates_none(client):
    eid = event_id_from_op_key("op-1")
    route = respx.post(f"{CAL}/calendars/primary/events").mock(return_value=httpx.Response(200, json=gevent(eid)))
    body = {"summary": "Focus", "start": {"dateTime": "2026-10-01T09:00:00-04:00"},
            "end": {"dateTime": "2026-10-01T10:00:00-04:00"}, "extendedProperties": {"private": {"jarvis_op": "op-1"}}}
    ev = GoogleCalendarAdapter(make_creds(client)).insert_event("primary", eid, body)
    req = route.calls[0].request
    assert req.url.params["sendUpdates"] == "none" and json.loads(req.content)["id"] == eid
    assert ev.id == eid


@respx.mock
def test_insert_409_returns_existing_when_ours(client):
    eid = event_id_from_op_key("op-1")
    respx.post(f"{CAL}/calendars/primary/events").mock(return_value=httpx.Response(409, json={
        "error": {"code": 409, "message": "The requested identifier already exists.", "errors": [{"reason": "duplicate"}]}}))
    respx.get(f"{CAL}/calendars/primary/events/{eid}").mock(return_value=httpx.Response(200, json=gevent(
        eid, extendedProperties={"private": {"jarvis_op": "op-1"}})))
    body = {"summary": "Focus", "extendedProperties": {"private": {"jarvis_op": "op-1"}}}
    assert GoogleCalendarAdapter(make_creds(client)).insert_event("primary", eid, body).id == eid


@respx.mock
def test_insert_409_foreign_event_is_conflict(client):
    eid = event_id_from_op_key("op-1")
    respx.post(f"{CAL}/calendars/primary/events").mock(return_value=httpx.Response(409, json={"error": {}}))
    respx.get(f"{CAL}/calendars/primary/events/{eid}").mock(return_value=httpx.Response(200, json=gevent(
        eid, extendedProperties={"private": {"jarvis_op": "other"}})))
    with pytest.raises(ProviderError) as e:
        GoogleCalendarAdapter(make_creds(client)).insert_event(
            "primary", eid, {"summary": "x", "extendedProperties": {"private": {"jarvis_op": "op-1"}}})
    assert e.value.kind == "conflict"


def test_attendees_refused_without_network(client):
    adapter = GoogleCalendarAdapter(make_creds(client))
    with pytest.raises(ProviderError) as e:
        adapter.insert_event("primary", "abcde12345", {"summary": "x", "attendees": [{"email": "b@x.com"}]})
    assert e.value.kind == "unsupported"
    with pytest.raises(ProviderError):
        adapter.patch_event("primary", "abcde12345", {"attendees": [{"email": "b@x.com"}]}, None)
    with pytest.raises(ProviderError) as e:
        adapter.insert_event("primary", "BAD_ID!", {"summary": "x"})
    assert e.value.kind == "validation"


@respx.mock
def test_patch_uses_if_match_and_412(client):
    route = respx.patch(f"{CAL}/calendars/primary/events/ev1").mock(return_value=httpx.Response(412, json={
        "error": {"code": 412, "message": "Precondition Failed"}}))
    with pytest.raises(ProviderError) as e:
        GoogleCalendarAdapter(make_creds(client)).patch_event("primary", "ev1", {"summary": "y"}, '"e1"')
    assert e.value.kind == "precondition_failed"
    req = route.calls[0].request
    assert req.headers["If-Match"] == '"e1"' and req.url.params["sendUpdates"] == "none"


@respx.mock
@pytest.mark.parametrize("status", [404, 410])
def test_delete_already_gone_is_ok(client, status):
    route = respx.delete(f"{CAL}/calendars/primary/events/ev1").mock(return_value=httpx.Response(status))
    GoogleCalendarAdapter(make_creds(client)).delete_event("primary", "ev1", '"e1"')
    assert route.calls[0].request.headers["If-Match"] == '"e1"'


@respx.mock
def test_calendar_write_errors(client):
    adapter = GoogleCalendarAdapter(make_creds(client))
    respx.patch(f"{CAL}/calendars/primary/events/ev1").mock(side_effect=[
        httpx.Response(503), httpx.ReadTimeout("slow"),
        httpx.Response(429, headers={"Retry-After": "12"}, json={"error": {}}),
        httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})])
    kinds = []
    for _ in range(4):
        with pytest.raises(ProviderError) as e:
            adapter.patch_event("primary", "ev1", {"summary": "y"}, None)
        kinds.append((e.value.kind, e.value.outcome_known, e.value.retry_after))
    assert kinds == [("server", False, None), ("timeout", False, None), ("rate_limit", True, 12.0),
                     ("rate_limit", True, None)]


@respx.mock
def test_get_event_404_none(client):
    respx.get(f"{CAL}/calendars/primary/events/nope").mock(return_value=httpx.Response(404, json={"error": {}}))
    assert GoogleCalendarAdapter(make_creds(client)).get_event("primary", "nope") is None


# ------------------------------------------------------------------ Drive

@respx.mock
def test_drive_list_and_search_queries(client):
    route = respx.get(f"{DRIVE}/files").mock(side_effect=[
        httpx.Response(200, json={"files": [{"id": "f1"}], "nextPageToken": "n"}),
        httpx.Response(200, json={"files": [{"id": "f2"}]}),
        httpx.Response(200, json={"files": [{"id": "f3"}]})])
    d = GoogleDriveAdapter(make_creds(client))
    assert [f["id"] for f in d.list_folder("fold1")] == ["f1", "f2"]
    assert route.calls[0].request.url.params["q"] == "'fold1' in parents and trashed=false"
    assert route.calls[1].request.url.params["pageToken"] == "n"
    d.search("AJ's \\ notes", ["fA", "fB"])
    assert route.calls[2].request.url.params["q"] == (
        "fullText contains 'AJ\\'s \\\\ notes' and trashed=false and ('fA' in parents or 'fB' in parents)")
    assert d.search("x", []) == []
    with pytest.raises(ProviderError):
        d.list_folder("x' or 'a")
    assert not any(hasattr(d, m) for m in ("create", "update", "delete", "upload", "trash"))


@respx.mock
@pytest.mark.parametrize("native,target", [
    ("application/vnd.google-apps.document", "text/plain"),
    ("application/vnd.google-apps.spreadsheet", "text/csv"),
    ("application/vnd.google-apps.presentation", "application/pdf")])
def test_drive_export_mime_mapping(client, native, target):
    route = respx.get(f"{DRIVE}/files/doc1/export").mock(return_value=httpx.Response(200, content=b"hello"))
    data, mime = GoogleDriveAdapter(make_creds(client)).download("doc1", native, 1000)
    assert (data, mime) == (b"hello", target)
    assert route.calls[0].request.url.params["mimeType"] == target


@respx.mock
def test_drive_download_media_and_byte_limit(client):
    respx.get(f"{DRIVE}/files/pdf1").mock(return_value=httpx.Response(
        200, content=b"x" * 50, headers={"Content-Type": "application/pdf"}))
    d = GoogleDriveAdapter(make_creds(client))
    data, mime = d.download("pdf1", "application/pdf", 100)
    assert len(data) == 50 and mime == "application/pdf"
    with pytest.raises(ProviderError) as e:
        d.download("pdf1", "application/pdf", 10)
    assert e.value.kind == "validation"


@respx.mock
def test_drive_download_limit_without_content_length(client):
    def stream_resp(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"y" * 5000))  # no declared length checked
    respx.get(f"{DRIVE}/files/big").mock(side_effect=stream_resp)
    with pytest.raises(ProviderError) as e:
        GoogleDriveAdapter(make_creds(client)).download("big", "text/plain", 1000)
    assert "max_bytes" in e.value.message


@respx.mock
def test_drive_404_and_unsupported_type(client):
    respx.get(f"{DRIVE}/files/missing").mock(return_value=httpx.Response(404, json={"error": {"message": "nf"}}))
    d = GoogleDriveAdapter(make_creds(client))
    with pytest.raises(ProviderError) as e:
        d.get_file("missing")
    assert e.value.kind == "not_found"
    with pytest.raises(ProviderError) as e:
        d.download("form1", "application/vnd.google-apps.form", 100)
    assert e.value.kind == "unsupported"
