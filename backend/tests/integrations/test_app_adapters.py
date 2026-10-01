"""Real V4 app adapters against mocked HTTP (respx). They prove request shapes/parsing/error mapping, not live access."""
import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from jarvis.integrations.base import ProviderError
from jarvis.integrations.canvas import CanvasAdapter
from jarvis.integrations.homeassistant import HomeAssistantAdapter, UnsafeHAURL, is_secure, validate_base_url
from jarvis.integrations.notion import API as NOTION
from jarvis.integrations.notion import NotionAdapter, paragraphs
from jarvis.integrations.routing import RoutingAdapter
from jarvis.integrations.simplefin import SetupTokenError, SimpleFinAdapter, claim, claim_url_from_token
from jarvis.integrations.slack import SlackAdapter
from jarvis.integrations.spotify import SpotifyAdapter, SpotifyCredentials


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c


@respx.mock
def test_notion_search_read_create(client):
    s = respx.post(f"{NOTION}/search").mock(return_value=httpx.Response(200, json={"results": [
        {"id": "p1", "url": "u", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Thesis"}]}}}]}))
    respx.get(f"{NOTION}/pages/p1").mock(return_value=httpx.Response(200, json={"id": "p1", "properties": {
        "title": {"type": "title", "title": [{"plain_text": "Thesis"}]}}}))
    respx.get(f"{NOTION}/blocks/p1/children").mock(side_effect=[
        httpx.Response(200, json={"results": [{"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Plan"}]}}],
                                  "has_more": True, "next_cursor": "c2"}),
        httpx.Response(200, json={"results": [{"type": "to_do", "to_do": {"checked": True, "rich_text": [{"plain_text": "IRB"}]}}],
                                  "has_more": False})])
    create = respx.post(f"{NOTION}/pages").mock(return_value=httpx.Response(200, json={"id": "p2", "url": "u2"}))
    n = NotionAdapter("secret_x" * 3, client=client)
    assert n.search("thesis")[0]["title"] == "Thesis"
    assert s.calls[0].request.headers["Notion-Version"] == "2022-06-28"
    assert n.read_page("p1")["text"] == "## Plan\n[x] IRB"
    n.create_page("p1", "Ch 2", "a" * 4000)
    body = json.loads(create.calls[0].request.content)
    assert body["parent"] == {"page_id": "p1"} and len(body["children"]) == 3      # split under Notion's 2000-char limit
    assert len(paragraphs("x\n\n" * 500)) == 100


@respx.mock
def test_slack_ok_false_is_mapped_and_post_carries_metadata(client):
    respx.get("https://slack.com/api/auth.test").mock(return_value=httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))
    post = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"}))
    s = SlackAdapter("xoxp-1", client=client)
    with pytest.raises(ProviderError) as e:
        s.whoami()
    assert e.value.kind == "auth"
    s.post("C1", "hello", "op_5")
    assert json.loads(post.calls[0].request.content)["metadata"] == {"event_type": "jarvis_op", "event_payload": {"op": "op_5"}}


@respx.mock
def test_canvas_pagination_never_leaves_configured_host(client):
    base = "https://uah.instructure.com"
    respx.get(f"{base}/api/v1/courses").mock(return_value=httpx.Response(
        200, json=[{"id": 1, "name": "IS 640", "course_code": "IS-640"}],
        headers={"Link": '<https://evil.example/api/v1/courses?page=2>; rel="next"'}))
    evil = respx.get("https://evil.example/api/v1/courses").mock(return_value=httpx.Response(200, json=[]))
    c = CanvasAdapter(base, "tok", client=client)
    assert [x["code"] for x in c.courses()] == ["IS-640"] and not evil.called


def test_home_assistant_url_and_tiers():
    fake_dns = lambda host, port, *a, **k: [(0, 0, 0, "", ("192.168.1.20" if host == "ha.lan" else "169.254.169.254", port))]  # noqa: E731
    assert validate_base_url("http://ha.lan:8123/x", resolver=fake_dns) == "http://ha.lan:8123"
    with pytest.raises(UnsafeHAURL):
        validate_base_url("http://metadata.internal", resolver=fake_dns)
    with pytest.raises(UnsafeHAURL):
        validate_base_url("http://user:pw@ha.lan:8123", resolver=fake_dns)
    assert not is_secure("light.office") and is_secure("lock.front") and is_secure("cover.garage") and is_secure("script.x")


@respx.mock
def test_home_assistant_service_call_shape(client):
    call = respx.post("http://ha.lan:8123/api/services/light/turn_on").mock(return_value=httpx.Response(200, json=[]))
    HomeAssistantAdapter("http://ha.lan:8123", "tok", client=client).call("light", "turn_on", "light.office", {"brightness_pct": 50})
    assert json.loads(call.calls[0].request.content) == {"entity_id": "light.office", "brightness_pct": 50}
    assert call.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_routing_geocode_and_route(client):
    respx.get("https://api.openrouteservice.org/geocode/search").mock(return_value=httpx.Response(200, json={
        "features": [{"geometry": {"coordinates": [-86.64, 34.72]}, "properties": {"label": "UAH"}}]}))
    rt = respx.post("https://api.openrouteservice.org/v2/directions/driving-car").mock(return_value=httpx.Response(
        200, json={"routes": [{"summary": {"duration": 1080, "distance": 9978}}]}))
    r = RoutingAdapter("key", client=client)
    d = r.geocode("UAH")
    assert (d["lat"], d["lon"]) == (34.72, -86.64)
    assert r.route((34.7, -86.5), (d["lat"], d["lon"])) == {"minutes": 18.0, "miles": 6.2, "mode": "drive"}
    assert json.loads(rt.calls[0].request.content)["coordinates"] == [[-86.5, 34.7], [-86.64, 34.72]]   # lon,lat order


@respx.mock
def test_simplefin_claim_and_accounts_hide_credentials(client):
    tok = base64.b64encode(b"https://bridge.simplefin.org/simplefin/claim/abc").decode()
    assert claim_url_from_token(tok).endswith("/claim/abc")
    with pytest.raises(SetupTokenError):
        claim_url_from_token(base64.b64encode(b"http://insecure").decode())
    respx.post("https://bridge.simplefin.org/simplefin/claim/abc").mock(
        return_value=httpx.Response(200, text="https://user:pass@bridge.simplefin.org/simplefin"))
    acc = respx.get("https://bridge.simplefin.org/simplefin/accounts").mock(return_value=httpx.Response(200, json={
        "errors": [], "accounts": [{"id": "a", "name": "Checking", "balance": "12.50", "org": {"name": "FCU"},
                                    "transactions": [{"id": "t", "amount": "-3.10", "description": "Coffee", "posted": 1}]}]}))
    public_dns = lambda host, port, *a, **k: [(0, 0, 0, "", ("93.184.216.34", port))]  # noqa: E731
    access = claim("https://bridge.simplefin.org/simplefin/claim/abc", client=client, resolver=public_dns)
    sf = SimpleFinAdapter(access, client=client)
    assert "pass" not in repr(sf)
    data = sf.accounts(1, 2)
    assert data["accounts"][0]["balance"] == 12.5 and data["accounts"][0]["transactions"][0]["amount"] == -3.1
    req = acc.calls[0].request
    assert req.headers["Authorization"].startswith("Basic ") and "pass" not in str(req.url)
    assert parse_qs(urlparse(str(req.url)).query) == {"start-date": ["1"], "end-date": ["2"]}


@respx.mock
def test_spotify_nothing_playing_and_play_body(client):
    respx.get("https://api.spotify.com/v1/me/player").mock(return_value=httpx.Response(204))
    play = respx.put("https://api.spotify.com/v1/me/player/play").mock(return_value=httpx.Response(204))
    sp = SpotifyAdapter(SpotifyCredentials({"access_token": "a", "refresh_token": "r", "expires_at": 9e12}, "cid",
                                           http_client=client))
    assert sp.now_playing() == {"active": False}
    sp.play("spotify:playlist:x")
    sp.play("spotify:track:y")
    assert [json.loads(c.request.content) for c in play.calls] == [{"context_uri": "spotify:playlist:x"},
                                                                   {"uris": ["spotify:track:y"]}]
