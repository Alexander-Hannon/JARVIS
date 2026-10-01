"""Notion adapter (REST, internal-integration token).

API (https://developers.notion.com/reference): header `Notion-Version: 2022-06-28`.
* GET  /v1/users/me                         -> the integration bot (validates the token)
* POST /v1/search {query, page_size}        -> pages the integration was shared with (Notion's own access scoping)
* GET  /v1/blocks/{id}/children             -> page content (paginated, `start_cursor`)
* POST /v1/pages {parent:{page_id}, properties:{title}, children}
* PATCH /v1/blocks/{id}/children {children} -> append

Access model: Notion only exposes pages AJ explicitly shared with the integration; JARVIS additionally writes only under
parent pages AJ granted (`notion_page` write grants). Notion has no idempotency key: page creation is reconciled by
looking for a child page with the exact title under the parent; appends can't be reconciled (idempotency `none`).
"""
from __future__ import annotations

import httpx

from .http import JsonClient

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
MAX_BLOCK_TEXT = 1900        # Notion rich_text content limit is 2000 chars per text object


def _plain(rich: list[dict] | None) -> str:
    return "".join(r.get("plain_text") or (r.get("text") or {}).get("content", "") for r in rich or [])


def title_of(page: dict) -> str:
    for p in (page.get("properties") or {}).values():
        if p.get("type") == "title":
            return _plain(p.get("title")) or "Untitled"
    return _plain((page.get("child_page") or {}).get("title") and [{"plain_text": page["child_page"]["title"]}]) or "Untitled"


def block_text(b: dict) -> str:
    t = b.get("type", "")
    body = b.get(t) or {}
    text = _plain(body.get("rich_text"))
    if t.startswith("heading_"):
        return "#" * int(t[-1]) + " " + text
    if t in ("bulleted_list_item", "numbered_list_item"):
        return "- " + text
    if t == "to_do":
        return ("[x] " if body.get("checked") else "[ ] ") + text
    if t == "child_page":
        return f"[page] {body.get('title', '')}"
    return text


def paragraphs(text: str) -> list[dict]:
    out = []
    for para in [p for p in text.split("\n\n") if p.strip()] or [text]:
        for i in range(0, len(para), MAX_BLOCK_TEXT):
            out.append({"object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": para[i:i + MAX_BLOCK_TEXT]}}]}})
    return out[:100]   # Notion accepts at most 100 children per request


class NotionAdapter:
    name = "notion"
    simulated = False

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.http = JsonClient(API, {"Authorization": f"Bearer {token}", "Notion-Version": VERSION}, "Notion", client=client)

    def whoami(self) -> dict:
        me = self.http.get("/users/me")
        return {"id": me.get("id"), "name": me.get("name") or "Notion integration",
                "workspace": (me.get("bot") or {}).get("workspace_name")}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        r = self.http.request("POST", "/search", json={"query": query, "page_size": max(1, min(limit, 50)),
                                                        "filter": {"property": "object", "value": "page"}})
        return [{"id": p["id"], "title": title_of(p), "url": p.get("url"), "last_edited": p.get("last_edited_time")}
                for p in r.get("results") or []]

    def children(self, block_id: str, max_blocks: int = 300) -> list[dict]:
        out, cursor = [], None
        while len(out) < max_blocks:
            params = {"page_size": 100} | ({"start_cursor": cursor} if cursor else {})
            r = self.http.get(f"/blocks/{block_id}/children", params=params)
            out += r.get("results") or []
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        return out[:max_blocks]

    def read_page(self, page_id: str) -> dict:
        page = self.http.get(f"/pages/{page_id}")
        text = "\n".join(t for t in (block_text(b) for b in self.children(page_id)) if t)
        return {"id": page["id"], "title": title_of(page), "url": page.get("url"), "text": text[:20000]}

    def find_child_page(self, parent_id: str, title: str) -> dict | None:
        for b in self.children(parent_id, 500):
            if b.get("type") == "child_page" and (b.get("child_page") or {}).get("title") == title and not b.get("archived"):
                return {"id": b["id"], "title": title}
        return None

    def create_page(self, parent_id: str, title: str, text: str) -> dict:
        p = self.http.request("POST", "/pages", write=True, json={
            "parent": {"page_id": parent_id}, "properties": {"title": {"title": [{"text": {"content": title[:200]}}]}},
            "children": paragraphs(text)})
        return {"id": p["id"], "url": p.get("url"), "title": title}

    def append(self, page_id: str, text: str) -> None:
        self.http.request("PATCH", f"/blocks/{page_id}/children", write=True, json={"children": paragraphs(text)})
