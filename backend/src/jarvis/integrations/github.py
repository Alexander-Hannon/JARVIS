"""GitHub adapter (RepoStatusProvider), read-only.

Authentication: a fine-grained personal access token limited to the selected repositories with
read-only repository permissions **Metadata**, **Issues**, and **Pull requests** (no write permissions,
no account permissions). Classic PATs are not recommended (``repo`` scope grants write access).
No pushing, merging or issue creation exists in this adapter.

Uses ``X-GitHub-Api-Version: 2022-11-28`` (supported versions checked 2026-09-28 at
https://docs.github.com/en/rest/about-the-rest-api/api-versions: 2026-03-10 and 2022-11-28).
Primary rate limit exhaustion: 403/429 with ``x-ratelimit-remaining: 0`` (reset in ``x-ratelimit-reset``);
secondary limits may send ``retry-after``.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .base import ProviderError

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
PAGE = 20
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _error(resp: httpx.Response) -> ProviderError:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:300]
    msg = body.get("message") if isinstance(body, dict) else str(body)
    code = resp.status_code
    h = resp.headers
    if code in (403, 429) and (h.get("x-ratelimit-remaining") == "0" or h.get("retry-after") or code == 429):
        retry_after: float | None = None
        if h.get("retry-after"):
            retry_after = float(h["retry-after"])
        elif h.get("x-ratelimit-reset", "").isdigit():
            retry_after = max(0.0, float(h["x-ratelimit-reset"]) - time.time())
        return ProviderError("rate_limit", f"GitHub rate limit: {msg}", status=code, retry_after=retry_after,
                             detail=body)
    if code == 401:
        return ProviderError("auth", f"GitHub token invalid/expired: {msg}", status=code, detail=body)
    if code == 403:
        return ProviderError("forbidden", f"GitHub forbidden (token lacks repo access?): {msg}", status=code,
                             detail=body)
    if code == 404:
        return ProviderError("not_found", f"GitHub repo not found or not granted to token: {msg}", status=code,
                             detail=body)
    if code >= 500:
        return ProviderError("server", f"GitHub server error {code}", status=code, detail=body)
    return ProviderError("validation", f"GitHub rejected request: {msg}", status=code, detail=body)


class GitHubAdapter:
    name = "github"
    simulated = False

    def __init__(self, token: str, client: httpx.Client | None = None, *, base_url: str = API_BASE):
        if not token:
            raise ProviderError("config", "GitHub token is not configured")
        self._token = token
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15.0)

    def __repr__(self) -> str:
        return "GitHubAdapter(token=***)"

    def _get(self, path: str, params: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION}
        try:
            resp = self._client.get(f"{self._base}{path}", params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"GitHub timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError("network", f"GitHub network error: {exc}") from exc
        if resp.status_code >= 400:
            raise _error(resp)
        return resp.json()

    def repo_status(self, full_name: str) -> dict:
        if not _REPO_RE.match(full_name or ""):
            raise ProviderError("validation", f"repository must be 'owner/name', got {full_name!r}")
        repo = self._get(f"/repos/{full_name}")
        issues = self._get(f"/repos/{full_name}/issues",
                           {"state": "open", "per_page": PAGE, "sort": "updated", "direction": "desc"})
        prs = self._get(f"/repos/{full_name}/pulls",
                        {"state": "open", "per_page": PAGE, "sort": "updated", "direction": "desc"})
        return {
            "repo": repo.get("full_name", full_name),
            "url": repo.get("html_url"),
            "default_branch": repo.get("default_branch"),
            "pushed_at": repo.get("pushed_at"),
            "private": repo.get("private"),
            "archived": repo.get("archived"),
            "open_issues": [
                {"number": i["number"], "title": i.get("title"), "url": i.get("html_url"),
                 "updated_at": i.get("updated_at"), "user": (i.get("user") or {}).get("login"),
                 "labels": [lb.get("name") for lb in i.get("labels") or [] if isinstance(lb, dict)]}
                for i in issues if "pull_request" not in i  # the issues endpoint also returns PRs
            ],
            "open_prs": [
                {"number": p["number"], "title": p.get("title"), "url": p.get("html_url"),
                 "updated_at": p.get("updated_at"), "user": (p.get("user") or {}).get("login"),
                 "draft": bool(p.get("draft")), "head": (p.get("head") or {}).get("ref")}
                for p in prs
            ],
        }
