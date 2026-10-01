"""SimpleFIN Bridge (read-only bank data; https://www.simplefin.org/protocol.html).

Setup: AJ creates a *setup token* at bridge.simplefin.org (base64 of a one-time claim URL). JARVIS POSTs to the claim
URL once and receives an *access URL* with HTTP basic credentials embedded; that URL is the secret (stored encrypted,
never logged). Data: GET {access_url}/accounts?start-date=<unix>&end-date=<unix> -> {"errors": [...], "accounts":
[{id, name, currency, balance, "balance-date", org:{name}, transactions:[{id, posted, amount, description, pending}]}]}.
The protocol is read-only by design: there is no way to move money through it.
"""
from __future__ import annotations

import base64
import binascii
from urllib.parse import urlsplit, urlunsplit

import httpx

from .base import ProviderError
from .http import map_http_error


class SetupTokenError(ValueError):
    pass


def claim_url_from_token(setup_token: str) -> str:
    try:
        url = base64.b64decode(setup_token.strip(), validate=True).decode().strip()
    except (binascii.Error, UnicodeDecodeError) as e:
        raise SetupTokenError("That isn't a SimpleFIN setup token (expected base64).") from e
    if not url.startswith("https://"):
        raise SetupTokenError("The setup token doesn't contain an https claim URL.")
    return url


def claim(claim_url: str, client: httpx.Client | None = None, resolver=None) -> str:
    import socket

    from jarvis.security.netguard import UnsafeURL, validate_url
    try:
        validate_url(claim_url, resolver=resolver or socket.getaddrinfo)
    except UnsafeURL as e:
        raise SetupTokenError(str(e)) from e
    c = client or httpx.Client(timeout=20.0)
    try:
        resp = c.post(claim_url, headers={"Content-Length": "0"})
    finally:
        if client is None:
            c.close()
    if resp.status_code == 403:
        raise SetupTokenError("That setup token was already claimed or is invalid; create a new one.")
    if resp.status_code >= 400:
        raise map_http_error(resp, write=False, vendor="SimpleFIN")
    access = resp.text.strip()
    if not access.startswith("https://"):
        raise SetupTokenError("SimpleFIN returned an unexpected access URL.")
    return access


class SimpleFinAdapter:
    name = "simplefin"
    simulated = False

    def __init__(self, access_url: str, client: httpx.Client | None = None):
        p = urlsplit(access_url)
        self._auth = (p.username or "", p.password or "")
        self._base = urlunsplit((p.scheme, p.hostname + (f":{p.port}" if p.port else ""), p.path.rstrip("/"), "", ""))
        self.client = client or httpx.Client(timeout=30.0)

    def __repr__(self) -> str:
        return "SimpleFinAdapter(<access url hidden>)"

    def accounts(self, start_unix: int | None = None, end_unix: int | None = None) -> dict:
        params = {k: v for k, v in (("start-date", start_unix), ("end-date", end_unix)) if v}
        try:
            resp = self.client.get(self._base + "/accounts", params=params, auth=self._auth)
        except httpx.TransportError as exc:
            # never chain: the transport error text can contain the credentialed URL
            raise ProviderError("network", f"SimpleFIN unreachable ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise map_http_error(resp, write=False, vendor="SimpleFIN")
        body = resp.json()
        accts = []
        for a in body.get("accounts") or []:
            accts.append({"id": a.get("id"), "name": a.get("name"), "org": (a.get("org") or {}).get("name"),
                          "currency": a.get("currency", "USD"), "balance": float(a.get("balance") or 0),
                          "available": float(a["available-balance"]) if a.get("available-balance") else None,
                          "as_of": a.get("balance-date"),
                          "transactions": [{"id": t.get("id"), "posted": t.get("posted"), "amount": float(t.get("amount") or 0),
                                            "description": t.get("description") or t.get("payee") or "",
                                            "pending": bool(t.get("pending"))} for t in a.get("transactions") or []]})
        return {"accounts": accts, "errors": body.get("errors") or []}
