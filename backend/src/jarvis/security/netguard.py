"""SSRF guard for user-supplied URLs (ICS feeds). Only public HTTPS hosts unless explicitly allowlisted."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class UnsafeURL(ValueError):
    pass


def _is_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified or (a.version == 6 and a.ipv4_mapped and not _is_public(str(a.ipv4_mapped))))


def validate_url(url: str, allow_local_hosts: set[str] | None = None, resolver=socket.getaddrinfo) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if allow_local_hosts and host in allow_local_hosts:
        return url
    if parts.scheme not in ("https", "webcal"):
        raise UnsafeURL("Only https:// (or webcal://) feed URLs are accepted.")
    if not host or parts.username or parts.password:
        raise UnsafeURL("URL must have a host and no embedded credentials.")
    try:
        infos = resolver(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"Host could not be resolved: {host}") from e
    for info in infos:
        if not _is_public(info[4][0]):
            raise UnsafeURL("URL resolves to a private, loopback, link-local or metadata address.")
    return url.replace("webcal://", "https://", 1) if parts.scheme == "webcal" else url


def safe_get(url: str, max_bytes: int = 5 * 1024 * 1024, timeout: float = 20.0, max_redirects: int = 3,
             allow_local_hosts: set[str] | None = None, client: httpx.Client | None = None) -> httpx.Response:
    """GET with manual redirect handling; every hop is re-validated. Enforces a byte limit."""
    current = validate_url(url, allow_local_hosts)
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        for _ in range(max_redirects + 1):
            with client.stream("GET", current, headers={"User-Agent": "JARVIS-ICS/1.0"}) as resp:
                if resp.is_redirect:
                    loc = resp.headers.get("location", "")
                    current = validate_url(str(resp.url.join(loc)), allow_local_hosts)
                    continue
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise UnsafeURL(f"Response exceeded {max_bytes} bytes.")
                return httpx.Response(resp.status_code, headers=resp.headers, content=bytes(buf), request=resp.request)
        raise UnsafeURL("Too many redirects.")
    finally:
        if own:
            client.close()
