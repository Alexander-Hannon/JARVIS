"""Allowlisting HTTP(S) forward proxy for the sandbox (runs in the jarvis-egress container).

The sandbox sits on an --internal network: this proxy is its only route out. It reads /run/allow/allow.json
({"domains": [...], "until": <unix>}) on every connection, so JARVIS can open/close an approved window instantly.
Only CONNECT to :443 and plain HTTP to :80 are forwarded; everything else (and every non-allowlisted host) is refused.
"""
import asyncio
import json
import time

ALLOW = "/run/allow/allow.json"


def allowed(host: str) -> bool:
    try:
        with open(ALLOW) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    if time.time() > float(cfg.get("until") or 0):
        return False
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in cfg.get("domains") or [])


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def handle(cr, cw):
    try:
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 15)
    except Exception:
        cw.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin1")
    method, target, _ = (line.split(" ") + ["", ""])[:3]
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        port = int(port or 443)
    else:
        rest = target.split("://", 1)[-1]
        hostport = rest.split("/", 1)[0]
        host, _, p = hostport.partition(":")
        port = int(p or 80)
    if port not in (80, 443) or not allowed(host):
        print(f"DENY {host}:{port}", flush=True)
        cw.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await cw.drain()
        cw.close()
        return
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
    except Exception:
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        cw.close()
        return
    print(f"ALLOW {host}:{port}", flush=True)
    if method == "CONNECT":
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
    else:
        uw.write(head)
        await uw.drain()
    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))


async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", 3128)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
