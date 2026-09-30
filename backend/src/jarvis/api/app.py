"""FastAPI application: security middleware, auth dependency, static frontend serving."""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from jarvis.config import REPO_ROOT, get_settings
from jarvis.core.db import get_conn, tx
from jarvis.core.seed import seed
from jarvis.security import auth, secrets

log = logging.getLogger("jarvis.api")


def allowed_origins() -> set[str]:
    s = get_settings()
    out = {f"http://{h}:{s.port}" for h in ("localhost", "127.0.0.1")} | {"http://localhost:5173", "http://127.0.0.1:5173"}
    if s.public_origin:
        out.add(s.public_origin.rstrip("/"))
    return out


def allowed_hosts() -> set[str]:
    s = get_settings()
    hosts = set(s.allowed_hosts)
    if s.public_origin:
        hosts.add(urlsplit(s.public_origin).hostname or "")
    return hosts


def db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def require_session(request: Request, conn=Depends(db)) -> dict:
    s = auth.session_from_request(conn, request)
    if not s:
        raise HTTPException(401, "Authentication required.")
    auth.check_csrf(request, s)
    return s


def create_app() -> FastAPI:
    from jarvis.logsafe import configure
    configure()
    s = get_settings()
    s.ensure_dirs()
    secrets.ensure_key()
    conn = get_conn()
    with tx(conn):
        seed(conn)
    conn.close()

    app = FastAPI(title="JARVIS", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].strip("[]")
        if host not in allowed_hosts():
            return JSONResponse({"detail": "Host not allowed."}, status_code=421)   # DNS-rebinding defense
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin.rstrip("/") not in allowed_origins():
            return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403)
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from jarvis.api import routes
    app.include_router(routes.public)
    app.include_router(routes.router, dependencies=[Depends(require_session)])

    dist = REPO_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = (dist / path).resolve()
            if path and f.is_file() and f.is_relative_to(dist.resolve()):
                return FileResponse(f)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_ui():
            return JSONResponse({"detail": "Frontend not built. Run `npm run build` in frontend/ (see README)."})
    return app


def _dist() -> Path:
    return REPO_ROOT / "frontend" / "dist"
