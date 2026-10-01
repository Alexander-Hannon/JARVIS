"""Telegram Bot API adapter (https://core.telegram.org/bots/api). The bot token is part of every URL path, so URLs are
never logged and transport errors are never chained (their text contains the URL).

Methods used: getMe, getUpdates (short poll from the worker: outbound only, no webhook, no inbound port), sendMessage
(with inline keyboards), answerCallbackQuery, editMessageReplyMarkup, getFile + file download (voice notes).
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import ProviderError

API = "https://api.telegram.org"
MAX_TEXT = 4000      # Telegram's limit is 4096 characters per message


class TelegramAdapter:
    name = "telegram"
    simulated = False

    def __init__(self, token: str, client: httpx.Client | None = None):
        self._token = token
        self.client = client or httpx.Client(timeout=20.0)

    def __repr__(self) -> str:
        return "TelegramAdapter(<token hidden>)"

    def _call(self, method: str, write: bool = False, **params: Any) -> Any:
        try:
            resp = self.client.post(f"{API}/bot{self._token}/{method}", json=params)
        except httpx.TransportError as e:
            raise ProviderError("network" if not write else "timeout", f"Telegram {method} unreachable ({type(e).__name__})",
                                outcome_known=not write) from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not body.get("ok"):
            code = body.get("error_code") or resp.status_code
            kind = {401: "auth", 403: "forbidden", 404: "not_found", 429: "rate_limit"}.get(code, "validation")
            if code >= 500:
                kind = "server"
            raise ProviderError(kind, f"Telegram {method}: {body.get('description') or code}",
                                retry_after=(body.get("parameters") or {}).get("retry_after"),
                                outcome_known=not (write and code >= 500))
        return body.get("result")

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: int | None, limit: int = 50) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=0, limit=limit,
                          allowed_updates=["message", "callback_query"]) or []

    def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", write=True, **params)

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        except ProviderError:
            pass   # cosmetic

    def download_file(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        f = self._call("getFile", file_id=file_id)
        if (f.get("file_size") or 0) > max_bytes:
            raise ProviderError("validation", "File too large.")
        try:
            resp = self.client.get(f"{API}/file/bot{self._token}/{f['file_path']}")
        except httpx.TransportError as e:
            raise ProviderError("network", f"Telegram file download failed ({type(e).__name__})") from None
        if resp.status_code != 200:
            raise ProviderError("server", "Telegram file download failed.")
        return resp.content[:max_bytes]
