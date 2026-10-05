"""Canvas LMS adapter (read-only; personal access token from Account → Settings → New access token).

REST (https://canvas.instructure.com/doc/api/): GET /api/v1/users/self, /api/v1/courses?enrollment_state=active,
/api/v1/users/self/todo, /api/v1/courses/:id/assignments?bucket=upcoming|overdue, /api/v1/announcements?context_codes[].
Pagination follows the `Link: <...>; rel="next"` header. The base URL is AJ's school instance (e.g.
https://uah.instructure.com) and must pass the SSRF guard (public HTTPS host). JARVIS never submits, grades or posts.
"""
from __future__ import annotations

import re

import httpx

from .http import JsonClient

NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class CanvasAdapter:
    name = "canvas"
    simulated = False

    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        from jarvis.security.netguard import pinned_client
        self.http = JsonClient(self.base + "/api/v1", {"Authorization": f"Bearer {token}"}, "Canvas",
                               client=client or pinned_client())

    def _all(self, path: str, params: dict | list | None = None, cap: int = 200) -> list[dict]:
        out: list[dict] = []
        url = path
        while url and len(out) < cap:
            resp = self.http.client.get(url if url.startswith("http") else self.http.base + url, params=params,
                                        headers=self.http.headers)
            if resp.status_code >= 400:
                from .http import map_http_error
                raise map_http_error(resp, write=False, vendor="Canvas")
            out += resp.json()
            m = NEXT.search(resp.headers.get("link", ""))
            url, params = (m.group(1) if m else None), None
            if url and not url.startswith(self.base):
                break   # never follow pagination off the configured host
        return out[:cap]

    def whoami(self) -> dict:
        me = self.http.get("/users/self")
        return {"id": me.get("id"), "name": me.get("name"), "email": me.get("primary_email") or me.get("login_id")}

    def courses(self) -> list[dict]:
        return [{"id": c["id"], "name": c.get("name"), "code": c.get("course_code")}
                for c in self._all("/courses", {"enrollment_state": "active", "per_page": 50}) if c.get("name")]

    def todo(self) -> list[dict]:
        out = []
        for t in self._all("/users/self/todo", {"per_page": 50}):
            a = t.get("assignment") or {}
            out.append({"type": t.get("type"), "course_id": t.get("course_id"), "name": a.get("name") or t.get("title"),
                        "due_at": a.get("due_at"), "url": t.get("html_url") or a.get("html_url"),
                        "needs_grading": t.get("needs_grading_count")})
        return out

    def assignments(self, course_id: int | str, bucket: str = "upcoming") -> list[dict]:
        return [{"id": a["id"], "name": a.get("name"), "due_at": a.get("due_at"), "points": a.get("points_possible"),
                 "submitted": bool((a.get("submission") or {}).get("submitted_at")), "url": a.get("html_url"),
                 "course_id": course_id}
                for a in self._all(f"/courses/{course_id}/assignments",
                                   {"bucket": bucket, "per_page": 50, "include[]": "submission"})]

    def announcements(self, course_ids: list, limit: int = 10) -> list[dict]:
        if not course_ids:
            return []
        params = [("context_codes[]", f"course_{c}") for c in course_ids] + [("per_page", str(limit))]
        return [{"title": a.get("title"), "posted_at": a.get("posted_at"), "course": a.get("context_code"),
                 "message": re.sub(r"<[^>]+>", " ", a.get("message") or "")[:1500], "url": a.get("html_url")}
                for a in self._all("/announcements", params, cap=limit)]
