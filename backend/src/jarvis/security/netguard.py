"""SSRF guard for user-supplied URLs (ICS feeds, Canvas, SimpleFIN). Only public HTTPS hosts unless allowlisted.

Validation alone has a gap: httpx resolves the name again when it connects, so a DNS answer that changes between the
check and the connect (rebinding) could still reach a private address. `PinnedTransport` closes it by resolving once,
checking that answer, and connecting to exactly that IP while TLS still verifies the certificate for the hostname.
"""
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


def resolve_public(host: str, port: int, resolver=socket.getaddrinfo) -> str:
    """Resolve once; every answer must be public. Returns the address to connect to."""
    try:
        infos = resolver(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"Host could not be resolved: {host}") from e
    if not infos:
        raise UnsafeURL(f"Host could not be resolved: {host}")
    for info in infos:
        if not _is_public(info[4][0]):
            raise UnsafeURL("URL resolves to a private, loopback, link-local or metadata address.")
    return infos[0][4][0]


def validate_url(url: str, allow_local_hosts: set[str] | None = None, resolver=socket.getaddrinfo) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if allow_local_hosts and host in allow_local_hosts:
        return url
    if parts.scheme not in ("https", "webcal"):
        raise UnsafeURL("Only https:// (or webcal://) feed URLs are accepted.")
    if not host or parts.username or parts.password:
        raise UnsafeURL("URL must have a host and no embedded credentials.")
    resolve_public(host, parts.port or 443, resolver)
    return url.replace("webcal://", "https://", 1) if parts.scheme == "webcal" else url


class PinnedTransport(httpx.BaseTransport):
    """Every request (including each redirect hop and pagination link) is re-validated and sent to the IP that was
    checked. The Host header and TLS SNI/certificate check keep using the original hostname."""

    def __init__(self, allow_local_hosts: set[str] | None = None, resolver=socket.getaddrinfo,
                 inner: httpx.BaseTransport | None = None):
        self.allow = {h.lower() for h in (allow_local_hosts or set())}
        self.resolver = resolver
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.lower()
        if host in self.allow:
            return self.inner.handle_request(request)
        if request.url.scheme != "https":
            raise UnsafeURL("Only https:// URLs are accepted.")
        if request.url.userinfo:
            raise UnsafeURL("URL must have no embedded credentials.")
        ip = resolve_public(host, request.url.port or 443, self.resolver)
        request.url = request.url.copy_with(host=ip)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def pinned_client(timeout: float = 20.0, **kw) -> httpx.Client:
    """An httpx client for user-supplied endpoints (no redirects followed automatically)."""
    return httpx.Client(timeout=timeout, follow_redirects=False, transport=PinnedTransport(**kw))


def safe_get(url: str, max_bytes: int = 5 * 1024 * 1024, timeout: float = 20.0, max_redirects: int = 3,
             allow_local_hosts: set[str] | None = None, client: httpx.Client | None = None) -> httpx.Response:
    """GET with manual redirect handling; every hop is re-validated. Enforces a byte limit."""
    current = validate_url(url, allow_local_hosts)
    own = client is None
    client = client or pinned_client(timeout, allow_local_hosts=allow_local_hosts)
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
                # iter_bytes() already undid gzip/deflate: drop encoding/length headers so the body isn't decoded twice.
                headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "content-length",
                                                                                      "transfer-encoding")}
                return httpx.Response(resp.status_code, headers=headers, content=bytes(buf), request=resp.request)
        raise UnsafeURL("Too many redirects.")
    finally:
        if own:
            client.close()
