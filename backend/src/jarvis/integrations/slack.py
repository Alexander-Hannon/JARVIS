"""Slack adapter (Web API with a *user* token, so JARVIS sees what AJ sees and posts as AJ).

Methods (https://api.slack.com/methods): auth.test, search.messages, conversations.list, conversations.history,
conversations.replies, users.info, chat.postMessage. Slack returns HTTP 200 with {"ok": false, "error": ...} on
failure; that is mapped to ProviderError here.

User-token scopes: search:read, channels:history, groups:history, im:history, mpim:history, channels:read,
groups:read, im:read, users:read, chat:write.

Idempotency: chat.postMessage has no idempotency key, so every JARVIS post carries Slack message `metadata`
({event_type: "jarvis_op", event_payload: {op}}), which is invisible to readers; reconciliation scans recent history
with include_all_metadata=true for that op before any retry.
"""
from __future__ import annotations

import httpx

from .base import ProviderError
from .http import JsonClient

API = "https://slack.com/api"
USER_SCOPES = ["search:read", "channels:history", "groups:history", "im:history", "mpim:history", "channels:read",
               "groups:read", "im:read", "users:read", "chat:write"]
_ERR = {"invalid_auth": "auth", "not_authed": "auth", "token_revoked": "auth", "account_inactive": "auth",
        "missing_scope": "forbidden", "channel_not_found": "not_found", "not_in_channel": "forbidden",
        "ratelimited": "rate_limit", "is_archived": "conflict", "msg_too_long": "validation"}


class SlackAdapter:
    name = "slack"
    simulated = False

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.http = JsonClient(API, {"Authorization": f"Bearer {token}"}, "Slack", client=client)
        self._users: dict[str, str] = {}

    def _call(self, method: str, *, write: bool = False, **params) -> dict:
        r = (self.http.request("POST", f"/{method}", write=write, json=params) if write
             else self.http.get(f"/{method}", params=params))
        if not r.get("ok"):
            err = r.get("error", "unknown_error")
            raise ProviderError(_ERR.get(err, "validation"), f"Slack {method}: {err}", detail={"error": err})
        return r

    def whoami(self) -> dict:
        r = self._call("auth.test")
        return {"user_id": r.get("user_id"), "user": r.get("user"), "team": r.get("team"), "url": r.get("url")}

    def user_name(self, uid: str) -> str:
        if uid and uid not in self._users:
            try:
                u = self._call("users.info", user=uid)["user"]
                self._users[uid] = (u.get("profile") or {}).get("real_name") or u.get("name") or uid
            except ProviderError:
                self._users[uid] = uid
        return self._users.get(uid, uid)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        r = self._call("search.messages", query=query, count=max(1, min(limit, 50)), sort="timestamp")
        return [{"channel_id": (m.get("channel") or {}).get("id"), "channel": (m.get("channel") or {}).get("name"),
                 "user": m.get("username") or self.user_name(m.get("user", "")), "text": m.get("text", ""),
                 "ts": m.get("ts"), "permalink": m.get("permalink")} for m in (r.get("messages") or {}).get("matches") or []]

    def channels(self, limit: int = 200) -> list[dict]:
        r = self._call("conversations.list", types="public_channel,private_channel,im,mpim", limit=limit,
                       exclude_archived=True)
        return [{"id": c["id"], "name": c.get("name") or ("DM " + self.user_name(c.get("user", ""))),
                 "is_im": bool(c.get("is_im")), "is_private": bool(c.get("is_private"))} for c in r.get("channels") or []]

    def history(self, channel: str, limit: int = 30, include_metadata: bool = False) -> list[dict]:
        r = self._call("conversations.history", channel=channel, limit=max(1, min(limit, 200)),
                       include_all_metadata=include_metadata)
        return [{"user": self.user_name(m.get("user", "")), "text": m.get("text", ""), "ts": m.get("ts"),
                 "metadata": m.get("metadata")} for m in r.get("messages") or []]

    def find_post_by_op(self, channel: str, op_key: str) -> dict | None:
        for m in self.history(channel, 100, include_metadata=True):
            md = m.get("metadata") or {}
            if md.get("event_type") == "jarvis_op" and (md.get("event_payload") or {}).get("op") == op_key:
                return m
        return None

    def post(self, channel: str, text: str, op_key: str) -> dict:
        r = self._call("chat.postMessage", write=True, channel=channel, text=text,
                       metadata={"event_type": "jarvis_op", "event_payload": {"op": op_key}})
        return {"channel": r.get("channel"), "ts": r.get("ts")}
