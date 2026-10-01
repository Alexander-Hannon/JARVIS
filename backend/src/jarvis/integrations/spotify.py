"""Spotify adapter (Web API) + OAuth authorization-code-with-PKCE (no client secret stored).

Accounts: https://accounts.spotify.com/authorize, /api/token (refresh keeps working with PKCE; a new refresh token may
be returned and must replace the old one).
API (https://developer.spotify.com/documentation/web-api): GET /v1/me, GET /v1/me/player, PUT /v1/me/player/play,
PUT /v1/me/player/pause, POST /v1/me/player/next, POST /v1/me/player/queue?uri=, GET /v1/search.
Playback control requires Spotify Premium and an active device; the API returns 404 NO_ACTIVE_DEVICE otherwise.
Redirect URIs must use an explicit loopback IP (http://127.0.0.1:PORT/...) — Spotify rejects `localhost`.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import ProviderError
from .google_oauth import GoogleCredentials, transport_error
from .http import map_http_error

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ["user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing", "playlist-read-private"]


def build_auth_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
                                       "scope": " ".join(SCOPES), "state": state, "code_challenge_method": "S256",
                                       "code_challenge": challenge})


def _token(data: dict, client: httpx.Client | None) -> dict:
    c = client or httpx.Client(timeout=15.0)
    try:
        resp = c.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.TransportError as exc:
        raise transport_error(exc, write=False) from exc
    finally:
        if client is None:
            c.close()
    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if body.get("error") == "invalid_grant":
            raise ProviderError("auth", "Spotify grant invalid/revoked; reconnect required",
                                detail={"reconnect_required": True})
        raise map_http_error(resp, write=False, vendor="Spotify")
    return resp.json()


def exchange_code(client_id: str, redirect_uri: str, code: str, verifier: str, client: httpx.Client | None = None) -> dict:
    b = _token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": client_id,
                "code_verifier": verifier}, client)
    return {"access_token": b["access_token"], "refresh_token": b.get("refresh_token"),
            "expires_at": time.time() + float(b.get("expires_in", 3600)), "scope": b.get("scope", "")}


class SpotifyCredentials(GoogleCredentials):
    """Refresh-on-expiry / retry-once-on-401 against Spotify (public PKCE client: no secret)."""

    def __init__(self, tokens: dict, client_id: str, **kw: Any):
        super().__init__(tokens, client_id, "", **kw)

    def refresh(self) -> None:
        with self._lock:
            rt = self._tokens.get("refresh_token")
            if not rt:
                raise ProviderError("auth", "no refresh token stored; reconnect required")
            b = _token({"grant_type": "refresh_token", "refresh_token": rt, "client_id": self._client_id}, self.client)
            self._tokens = {"access_token": b["access_token"], "refresh_token": b.get("refresh_token") or rt,
                            "expires_at": self._clock() + float(b.get("expires_in", 3600)),
                            "scope": b.get("scope") or self._tokens.get("scope", "")}
            new = dict(self._tokens)
        if self._updater:
            self._updater(new)

    def request(self, method: str, url: str, *, write: bool = False, **kwargs: Any) -> httpx.Response:
        resp = self._send(method, url, write=write, **kwargs)
        if resp.status_code == 401:
            self.refresh()
            resp = self._send(method, url, write=write, **kwargs)
        if resp.status_code >= 400:
            raise map_http_error(resp, write=write, vendor="Spotify")
        return resp


def _track(t: dict | None) -> dict | None:
    if not t:
        return None
    return {"uri": t.get("uri"), "name": t.get("name"), "artists": ", ".join(a.get("name", "") for a in t.get("artists") or []),
            "album": (t.get("album") or {}).get("name")}


class SpotifyAdapter:
    name = "spotify"
    simulated = False

    def __init__(self, creds: SpotifyCredentials):
        self.creds = creds

    def whoami(self) -> dict:
        me = self.creds.request("GET", API + "/me").json()
        return {"id": me.get("id"), "name": me.get("display_name"), "product": me.get("product")}

    def now_playing(self) -> dict:
        resp = self.creds.request("GET", API + "/me/player")
        if resp.status_code == 204 or not resp.content:
            return {"active": False}
        p = resp.json()
        return {"active": True, "is_playing": p.get("is_playing"), "device": (p.get("device") or {}).get("name"),
                "track": _track(p.get("item")), "context": (p.get("context") or {}).get("uri"),
                "progress_ms": p.get("progress_ms")}

    def search(self, query: str, kind: str = "track", limit: int = 5) -> list[dict]:
        r = self.creds.request("GET", API + "/search", params={"q": query, "type": kind, "limit": limit}).json()
        items = (r.get(kind + "s") or {}).get("items") or []
        if kind == "track":
            return [_track(i) for i in items if i]
        return [{"uri": i.get("uri"), "name": i.get("name"), "owner": (i.get("owner") or {}).get("display_name")}
                for i in items if i]

    def play(self, uri: str | None = None) -> None:
        body = {} if not uri else ({"uris": [uri]} if ":track:" in uri else {"context_uri": uri})
        self.creds.request("PUT", API + "/me/player/play", json=body, write=True)

    def pause(self) -> None:
        self.creds.request("PUT", API + "/me/player/pause", write=True)

    def next(self) -> None:
        self.creds.request("POST", API + "/me/player/next", write=True)

    def queue(self, uri: str) -> None:
        self.creds.request("POST", API + "/me/player/queue", params={"uri": uri}, write=True)
