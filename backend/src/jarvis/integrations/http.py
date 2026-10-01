"""Shared JSON-over-HTTP client for token-authenticated adapters (Notion, Slack, Canvas, Home Assistant, ...).

One error model for every vendor: a write whose response is lost (timeout after send, 5xx) is `outcome_known=False`
so the executor reconciles before any retry; reads are always outcome-known.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import ProviderError
from .google_oauth import transport_error


def map_http_error(resp: httpx.Response, *, write: bool, vendor: str) -> ProviderError:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:300]
    msg = ""
    if isinstance(body, dict):
        e = body.get("error")
        msg = (body.get("message") or (e.get("message") if isinstance(e, dict) else e) or body.get("errors") or "")
    msg = str(msg)[:300] or f"HTTP {resp.status_code}"
    code = resp.status_code
    ra = resp.headers.get("Retry-After")
    retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
    kw: dict[str, Any] = {"status": code, "detail": body}
    if code == 401:
        return ProviderError("auth", f"{vendor} rejected the credentials: {msg}", **kw)
    if code == 403:
        return ProviderError("forbidden", f"{vendor} forbidden: {msg}", **kw)
    if code == 404:
        return ProviderError("not_found", f"{vendor} not found: {msg}", **kw)
    if code == 409:
        return ProviderError("conflict", f"{vendor} conflict: {msg}", **kw)
    if code == 429:
        return ProviderError("rate_limit", f"{vendor} rate limited: {msg}", retry_after=retry_after, **kw)
    if code >= 500:
        return ProviderError("server", f"{vendor} server error {code}", outcome_known=not write, retry_after=retry_after,
                             **kw)
    return ProviderError("validation", f"{vendor} rejected the request: {msg}", **kw)


class JsonClient:
    def __init__(self, base_url: str, headers: dict[str, str], vendor: str, *, client: httpx.Client | None = None,
                 timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.headers = headers
        self.vendor = vendor
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def __repr__(self) -> str:   # never leak tokens through reprs/logs
        return f"JsonClient({self.vendor}, {self.base})"

    def request(self, method: str, path: str, *, write: bool = False, **kw: Any) -> Any:
        url = path if path.startswith("http") else self.base + path
        headers = self.headers | kw.pop("headers", {})
        try:
            resp = self.client.request(method, url, headers=headers, **kw)
        except httpx.TransportError as exc:
            raise transport_error(exc, write=write) from exc
        if resp.status_code >= 400:
            raise map_http_error(resp, write=write, vendor=self.vendor)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"text": resp.text}

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)
