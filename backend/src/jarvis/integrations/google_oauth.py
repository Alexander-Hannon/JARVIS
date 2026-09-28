"""Google OAuth 2.0 (web-server flow + PKCE) helpers and a refreshing credentials holder. No Google SDK.

Verified 2026-09-28 against:
* https://developers.google.com/identity/protocols/oauth2/web-server -- auth endpoint
  ``https://accounts.google.com/o/oauth2/v2/auth``; token endpoint ``https://oauth2.googleapis.com/token``;
  revoke ``POST https://oauth2.googleapis.com/revoke`` (form ``token=``); refresh of a revoked/expired
  refresh token fails with ``invalid_grant``.
* https://developers.google.com/workspace/calendar/api/auth -- scope list (see CALENDAR_SCOPES).
* https://developers.google.com/workspace/drive/api/guides/api-specific-auth -- Drive scope classes.

Calendar scopes (least privilege for: list calendars, read events on selected calendars, free/busy,
create/update/delete events only on calendars AJ owns):
* ``calendar.calendarlist.readonly`` -- "See the list of Google calendars you're subscribed to."
* ``calendar.events.readonly`` -- read events on selected calendars AJ does not own (shared/course
  calendars). Read-only, so a shared calendar can never be written through OAuth either.
* ``calendar.events.owned`` -- "See, create, change, and delete events on Google calendars you own."
  Used instead of ``calendar.events`` (which grants edit on *all* calendars AJ can edit, incl. shared).
* ``calendar.freebusy`` -- "View your availability in your calendars." (accepted by freebusy.query).
* ``openid email`` -- to show which Google account is connected (never silently choose an account).
The app layer additionally restricts reads to AJ-selected calendars and writes to the one chosen
destination calendar.

Drive scope trade-off (DRIVE_SCOPES):
* ``drive.file`` (non-sensitive) grants access ONLY to files the app created or that AJ explicitly
  opened/shared with the app via the Google Picker. It is per-file authorization and must NOT be
  described as read access to arbitrary/existing Drive files or folders. Using it requires a Picker UI;
  folder contents are not implicitly included.
* ``drive.readonly`` (restricted) -- "View and download all your Drive files". Broad read of everything;
  Google classifies it as restricted (a security assessment applies to apps that store/transmit
  restricted-scope data server-side at scale; a single-user testing-mode project is the practical path).
Default here: ``drive.readonly`` so AJ can pick existing folders without a Picker, with the application
layer enforcing a selected-root allowlist (no whole-Drive indexing, no search outside selected roots).
``DRIVE_FILE_SCOPES`` is provided for the per-file alternative. No Drive write scope is ever requested.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import ProviderError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_G = "https://www.googleapis.com/auth/"
CALENDAR_SCOPES = ["openid", "email",
                   _G + "calendar.calendarlist.readonly",
                   _G + "calendar.events.readonly",
                   _G + "calendar.events.owned",
                   _G + "calendar.freebusy"]
DRIVE_SCOPES = ["openid", "email", _G + "drive.readonly"]
DRIVE_FILE_SCOPES = ["openid", "email", _G + "drive.file"]

REFRESH_SKEW_SECONDS = 60


# --------------------------------------------------------------------------- shared HTTP error mapping

def transport_error(exc: httpx.TransportError, *, write: bool) -> ProviderError:
    if isinstance(exc, httpx.ConnectTimeout):
        return ProviderError("timeout", f"connect timeout: {exc}", outcome_known=True)
    if isinstance(exc, httpx.ConnectError):
        return ProviderError("network", f"connect error: {exc}", outcome_known=True)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"timeout: {exc}", outcome_known=not write)
    return ProviderError("network", f"network error: {exc}", outcome_known=not write)


def google_api_error(resp: httpx.Response, *, write: bool) -> ProviderError:
    """Map a Google API (Calendar/Drive) error response to ProviderError."""
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    err = body.get("error") if isinstance(body, dict) else None
    message = (err.get("message") if isinstance(err, dict) else None) or f"HTTP {resp.status_code}"
    reasons = {e.get("reason") for e in (err.get("errors") or [])} if isinstance(err, dict) else set()
    code = resp.status_code
    retry_after = None
    if resp.headers.get("Retry-After"):
        try:
            retry_after = float(resp.headers["Retry-After"])
        except ValueError:
            retry_after = None
    kw: dict[str, Any] = {"status": code, "detail": body}
    if code == 401:
        return ProviderError("auth", f"Google auth failed: {message}", **kw)
    if code == 429 or (code == 403 and reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}):
        return ProviderError("rate_limit", f"Google rate limit: {message}", retry_after=retry_after, **kw)
    if code == 403:
        return ProviderError("forbidden", f"Google forbidden: {message}", **kw)
    if code in (404, 410):
        return ProviderError("not_found", f"Google not found/gone: {message}", **kw)
    if code == 409:
        return ProviderError("conflict", f"Google conflict: {message}", **kw)
    if code == 412:
        return ProviderError("precondition_failed", f"Google precondition failed (etag): {message}", **kw)
    if code >= 500:
        return ProviderError("server", f"Google server error {code}", outcome_known=not write,
                             retry_after=retry_after, **kw)
    return ProviderError("validation", f"Google rejected request: {message}", **kw)


def _token_error(resp: httpx.Response) -> ProviderError:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:200]}
    err = body.get("error") if isinstance(body, dict) else None
    desc = body.get("error_description", "") if isinstance(body, dict) else ""
    if err == "invalid_grant":
        return ProviderError("auth", f"Google grant invalid/revoked; reconnect required ({desc})",
                             status=resp.status_code, detail={"error": err, "reconnect_required": True})
    if err in ("invalid_client", "unauthorized_client", "redirect_uri_mismatch"):
        return ProviderError("config", f"Google OAuth client misconfigured: {err} {desc}",
                             status=resp.status_code, detail={"error": err})
    if resp.status_code >= 500:
        return ProviderError("server", f"Google token endpoint error {resp.status_code}", status=resp.status_code)
    if resp.status_code == 429:
        return ProviderError("rate_limit", "Google token endpoint rate limited", status=429)
    return ProviderError("validation", f"Google token request failed: {err} {desc}", status=resp.status_code,
                         detail={"error": err})


# --------------------------------------------------------------------------- flow helpers

def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def build_auth_url(client_id: str, redirect_uri: str, scopes: list[str], state: str, code_challenge: str,
                   *, login_hint: str | None = None) -> str:
    if not state or len(state) < 16:
        raise ProviderError("validation", "state must be an unguessable value (>=16 chars)")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_post(data: dict, client: httpx.Client | None) -> dict:
    c = client or httpx.Client(timeout=15.0)
    try:
        resp = c.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise transport_error(exc, write=False) from exc
    finally:
        if client is None:
            c.close()
    if resp.status_code != 200:
        raise _token_error(resp)
    return resp.json()


def _normalize_tokens(body: dict, now: float, previous_refresh: str | None = None) -> dict:
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token") or previous_refresh,
        "expires_at": now + float(body.get("expires_in", 3600)),
        "scope": body.get("scope", ""),
        "token_type": body.get("token_type", "Bearer"),
        "id_token": body.get("id_token"),
    }


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str, code_verifier: str,
                  *, client: httpx.Client | None = None, now: float | None = None) -> dict:
    """Exchange an authorization code. Returns {access_token, refresh_token, expires_at, scope, token_type, id_token}."""
    body = _token_post({"code": code, "client_id": client_id, "client_secret": client_secret,
                        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                        "code_verifier": code_verifier}, client)
    return _normalize_tokens(body, time.time() if now is None else now)


def refresh(client_id: str, client_secret: str, refresh_token: str, *, client: httpx.Client | None = None,
            now: float | None = None) -> dict:
    """Refresh an access token. invalid_grant -> ProviderError('auth') meaning reconnect required."""
    if not refresh_token:
        raise ProviderError("auth", "no refresh token stored; reconnect required",
                            detail={"reconnect_required": True})
    body = _token_post({"client_id": client_id, "client_secret": client_secret,
                        "refresh_token": refresh_token, "grant_type": "refresh_token"}, client)
    return _normalize_tokens(body, time.time() if now is None else now, previous_refresh=refresh_token)


def revoke(token: str, *, client: httpx.Client | None = None) -> None:
    """Revoke an access or refresh token (revoking the refresh token revokes the grant)."""
    c = client or httpx.Client(timeout=15.0)
    try:
        resp = c.post(REVOKE_URL, data={"token": token},
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.TransportError as exc:
        raise transport_error(exc, write=True) from exc
    finally:
        if client is None:
            c.close()
    if resp.status_code == 200:
        return
    if resp.status_code == 400:  # already invalid/revoked token -> treat as done
        return
    raise google_api_error(resp, write=True)


def fetch_userinfo(access_token: str, *, client: httpx.Client | None = None) -> dict:
    """OpenID userinfo (needs openid+email scopes): {sub, email, email_verified, ...}."""
    c = client or httpx.Client(timeout=15.0)
    try:
        resp = c.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.TransportError as exc:
        raise transport_error(exc, write=False) from exc
    finally:
        if client is None:
            c.close()
    if resp.status_code != 200:
        raise google_api_error(resp, write=False)
    return resp.json()


# --------------------------------------------------------------------------- credentials

class GoogleCredentials:
    """Holds Google tokens, auto-refreshes (expired or within 60s), and performs authorized requests.

    ``token_updater(tokens)`` is invoked with the full new token dict after every refresh so the caller
    can re-encrypt and persist it. Tokens are never included in repr/str.
    """

    def __init__(self, tokens: dict, client_id: str, client_secret: str, *,
                 token_updater: Callable[[dict], None] | None = None, http_client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        if not tokens.get("access_token") and not tokens.get("refresh_token"):
            raise ProviderError("config", "Google credentials have no tokens; connect Google first")
        self._tokens = dict(tokens)
        self._client_id = client_id
        self._client_secret = client_secret
        self._updater = token_updater
        self.client = http_client or httpx.Client(timeout=30.0)
        self._clock = clock
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"GoogleCredentials(scope={self.scopes!r}, expires_at={self._tokens.get('expires_at')})"

    @property
    def scopes(self) -> list[str]:
        return (self._tokens.get("scope") or "").split()

    @property
    def expired(self) -> bool:
        exp = self._tokens.get("expires_at")
        return not self._tokens.get("access_token") or exp is None or \
            self._clock() >= float(exp) - REFRESH_SKEW_SECONDS

    def refresh(self) -> None:
        with self._lock:
            new = refresh(self._client_id, self._client_secret, self._tokens.get("refresh_token") or "",
                          client=self.client, now=self._clock())
            if not new.get("id_token"):
                new["id_token"] = self._tokens.get("id_token")
            if not new.get("scope"):
                new["scope"] = self._tokens.get("scope", "")
            self._tokens = new
        if self._updater:
            self._updater(dict(new))

    def access_token(self) -> str:
        if self.expired:
            self.refresh()
        return self._tokens["access_token"]

    def request(self, method: str, url: str, *, write: bool = False, **kwargs: Any) -> httpx.Response:
        """Authorized request; refreshes once and retries on 401. Returns the response for any status
        < 400; raises ProviderError for errors (callers catch specific kinds)."""
        resp = self._send(method, url, write=write, **kwargs)
        if resp.status_code == 401:
            # A 401 means the request was rejected (not applied), so one retry is safe even for writes.
            self.refresh()
            resp = self._send(method, url, write=write, **kwargs)
        if resp.status_code >= 400:
            raise google_api_error(resp, write=write)
        return resp

    def _send(self, method: str, url: str, *, write: bool, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self.access_token()}"
        try:
            return self.client.request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise transport_error(exc, write=write) from exc
