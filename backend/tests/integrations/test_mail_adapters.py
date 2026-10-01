"""Gmail + Outlook adapters against mocked HTTP (respx): request shapes, parsing, hidden op marker, error mapping.
These prove the adapters speak the documented APIs; they are not live verification."""
import base64
import email
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from jarvis.integrations import google_oauth as go
from jarvis.integrations import microsoft as ms
from jarvis.integrations.base import ProviderError
from jarvis.integrations.gmail import API as GMAIL
from jarvis.integrations.gmail import OP_HEADER, GmailAdapter

NOW = 1_800_000_000.0


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c


def gcreds(client):
    return go.GoogleCredentials({"access_token": "at", "refresh_token": "rt", "expires_at": NOW + 3600, "scope": ""},
                                "cid", "cs", http_client=client, clock=lambda: NOW)


def mcreds(client, refreshed=None):
    return ms.MicrosoftCredentials({"access_token": "at", "refresh_token": "rt", "expires_at": NOW + 3600,
                                    "scope": " ".join(ms.MAIL_SCOPES)}, "cid", "cs", "common", http_client=client,
                                   clock=lambda: NOW, token_updater=refreshed)


GMAIL_FULL = {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "snippet": "can you send &amp; grades",
              "internalDate": "1790000000000",
              "payload": {"mimeType": "multipart/alternative", "headers": [
                  {"name": "From", "value": "Dr. Lee <Lee@UAH.edu>"}, {"name": "To", "value": "AJ <aj@example.test>"},
                  {"name": "Subject", "value": "Grades"}, {"name": "Message-ID", "value": "<abc@uah.edu>"}],
                  "parts": [{"mimeType": "text/html", "body": {"data": b64("<p>Hi&nbsp;AJ</p><script>x</script>")}},
                            {"mimeType": "text/plain", "body": {"data": b64("Hi AJ, can you send grades?")}}]}}


@respx.mock
def test_gmail_list_get_and_parse(client):
    respx.get(f"{GMAIL}/messages").mock(return_value=httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}]}))
    meta = respx.get(f"{GMAIL}/messages/m1").mock(return_value=httpx.Response(200, json=GMAIL_FULL))
    g = GmailAdapter(gcreds(client))
    [m] = g.list_messages(None, 10, unread_only=True)
    q = parse_qs(urlparse(str(respx.calls[0].request.url)).query)
    assert q["q"] == ["is:unread"] and q["maxResults"] == ["10"]
    assert "metadataHeaders" in parse_qs(urlparse(str(meta.calls[0].request.url)).query)
    assert m.sender_email == "lee@uah.edu" and m.unread and m.snippet == "can you send & grades" and m.body_text is None
    full = g.get_message("m1")
    assert full.body_text == "Hi AJ, can you send grades?"         # text/plain preferred over HTML
    assert full.message_id_header == "<abc@uah.edu>" and full.date.endswith("Z")


@respx.mock
def test_gmail_draft_carries_hidden_op_header_and_threads_reply(client):
    created = respx.post(f"{GMAIL}/drafts").mock(return_value=httpx.Response(200, json={"id": "d1", "message": {"id": "x"}}))

    def draft_get(request):
        raw = json.loads(created.calls[0].request.content)["message"]["raw"]
        mime = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=="))
        return httpx.Response(200, json={"id": "d1", "message": {"id": "x", "threadId": "t1", "payload": {
            "mimeType": "text/plain", "headers": [{"name": k, "value": v} for k, v in mime.items()],
            "body": {"data": b64(mime.get_payload())}}}})
    respx.get(f"{GMAIL}/drafts/d1").mock(side_effect=draft_get)
    from jarvis.integrations.gmail import to_message
    reply = to_message(GMAIL_FULL, with_body=True)
    d = GmailAdapter(gcreds(client)).create_draft("op_123", ["lee@uah.edu"], "Re: Grades", "By Friday. — AJ", reply_to=reply)
    sent = json.loads(created.calls[0].request.content)["message"]
    mime = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"] + "=="))
    assert sent["threadId"] == "t1" and mime[OP_HEADER] == "op_123" and mime["In-Reply-To"] == "<abc@uah.edu>"
    assert "op_123" not in mime.get_payload()                       # marker never in the visible body
    assert d.op_key == "op_123" and d.to == ["lee@uah.edu"]


@respx.mock
def test_gmail_find_by_op_and_delete_idempotent(client):
    respx.get(f"{GMAIL}/drafts").mock(return_value=httpx.Response(200, json={"drafts": [{"id": "d1"}, {"id": "d2"}]}))
    for did, op in (("d1", "other"), ("d2", "op_9")):
        respx.get(f"{GMAIL}/drafts/{did}").mock(return_value=httpx.Response(200, json={"id": did, "message": {
            "id": did, "payload": {"mimeType": "text/plain", "headers": [{"name": OP_HEADER, "value": op}],
                                   "body": {"data": b64("x")}}}}))
    respx.delete(f"{GMAIL}/drafts/gone").mock(return_value=httpx.Response(404, json={"error": {"message": "nf"}}))
    g = GmailAdapter(gcreds(client))
    assert g.find_draft_by_op("op_9").id == "d2"
    g.delete_draft("gone")                                          # already deleted = desired state, no error


@respx.mock
def test_gmail_write_5xx_is_unknown_outcome(client):
    respx.post(f"{GMAIL}/drafts").mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError) as e:
        GmailAdapter(gcreds(client)).create_draft("op", ["a@b.c"], "s", "b")
    assert e.value.kind == "server" and e.value.outcome_known is False


# ------------------------------------------------------------------ Microsoft
def test_microsoft_auth_url_pkce_and_tenant():
    url = ms.build_auth_url("common", "cid", "http://localhost:8000/api/integrations/microsoft/callback",
                            ms.MAIL_SCOPES, "s" * 32, "chal")
    p = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    assert p.netloc == "login.microsoftonline.com" and p.path == "/common/oauth2/v2.0/authorize"
    assert q["code_challenge_method"] == "S256" and "offline_access" in q["scope"] and "Mail.Send" not in q["scope"]


OUT_MSG = {"id": "o1", "conversationId": "c1", "subject": "Lab", "from": {"emailAddress": {"name": "Sam", "address": "Sam@x.org"}},
           "toRecipients": [{"emailAddress": {"address": "aj@outlook.com"}}], "receivedDateTime": "2026-09-28T13:00:00Z",
           "bodyPreview": "moved", "isRead": False, "importance": "high", "webLink": "https://outlook.live.com/x",
           "body": {"contentType": "html", "content": "<div>Room 210</div>"}}


@respx.mock
def test_outlook_list_search_and_body(client):
    inbox = respx.get(f"{ms.GRAPH}/me/mailFolders/inbox/messages").mock(return_value=httpx.Response(200, json={"value": [OUT_MSG]}))
    search = respx.get(f"{ms.GRAPH}/me/messages").mock(return_value=httpx.Response(200, json={"value": [OUT_MSG]}))
    respx.get(f"{ms.GRAPH}/me/messages/o1").mock(return_value=httpx.Response(200, json=OUT_MSG))
    o = ms.OutlookAdapter(mcreds(client))
    [m] = o.list_messages(None, 5, unread_only=True)
    q = parse_qs(urlparse(str(inbox.calls[0].request.url)).query)
    assert q["$filter"] == ["isRead eq false"] and q["$orderby"] == ["receivedDateTime desc"]
    assert m.sender_email == "sam@x.org" and m.unread and m.important
    o.list_messages('grades "now"', 5)
    assert parse_qs(urlparse(str(search.calls[0].request.url)).query)["$search"] == ['"grades now"']
    assert o.get_message("o1").body_text == "Room 210"


@respx.mock
def test_outlook_draft_uses_extended_property_and_reconciles(client):
    post = respx.post(f"{ms.GRAPH}/me/messages").mock(return_value=httpx.Response(201, json={"id": "dr1"}))
    draft = {"id": "dr1", "subject": "Hi", "toRecipients": [{"emailAddress": {"address": "lee@uah.edu"}}],
             "body": {"contentType": "text", "content": "Body"}, "isDraft": True,
             "singleValueExtendedProperties": [{"id": ms.OP_PROP, "value": "op_7"}]}
    respx.get(f"{ms.GRAPH}/me/messages/dr1").mock(return_value=httpx.Response(200, json=draft))
    found = respx.get(f"{ms.GRAPH}/me/mailFolders/drafts/messages").mock(
        return_value=httpx.Response(200, json={"value": [draft]}))
    o = ms.OutlookAdapter(mcreds(client))
    d = o.create_draft("op_7", ["lee@uah.edu"], "Hi", "Body")
    body = json.loads(post.calls[0].request.content)
    assert body["singleValueExtendedProperties"] == [{"id": ms.OP_PROP, "value": "op_7"}]
    assert body["body"] == {"contentType": "Text", "content": "Body"} and d.op_key == "op_7"
    assert o.find_draft_by_op("op_7").id == "dr1"
    assert "ep/value eq 'op_7'" in parse_qs(urlparse(str(found.calls[0].request.url)).query)["$filter"][0]


@respx.mock
def test_outlook_refreshes_on_401_and_maps_throttling(client):
    seen = []
    respx.post("https://login.microsoftonline.com/common/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at2", "expires_in": 3600, "scope": "Mail.ReadWrite"}))
    respx.get(f"{ms.GRAPH}/me/messages/o1").mock(side_effect=[httpx.Response(401), httpx.Response(200, json=OUT_MSG)])
    o = ms.OutlookAdapter(mcreds(client, refreshed=seen.append))
    assert o.get_message("o1").id == "o1" and seen[0]["refresh_token"] == "rt"   # kept when not rotated
    respx.get(f"{ms.GRAPH}/me/messages/o2").mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(ProviderError) as e:
        o.get_message("o2")
    assert e.value.kind == "rate_limit" and e.value.retry_after == 7.0


@respx.mock
def test_microsoft_invalid_grant_means_reconnect(client):
    respx.post("https://login.microsoftonline.com/common/oauth2/v2.0/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    c = ms.MicrosoftCredentials({"refresh_token": "rt", "expires_at": 0}, "cid", "cs", "common", http_client=client,
                                clock=lambda: NOW)
    with pytest.raises(ProviderError) as e:
        c.access_token()
    assert e.value.kind == "auth" and e.value.detail == {"reconnect_required": True}
