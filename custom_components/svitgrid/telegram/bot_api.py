"""A minimal Telegram Bot API client.

The token goes only into the request URL path. Nothing here puts the token or
the URL into an exception message, a log line or a `repr`: aiohttp errors can
quote the URL, so they are caught and replaced without chaining.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Any

import aiohttp

_API = "https://api.telegram.org"
_CALL_TIMEOUT_S = 15
_POLL_MARGIN_S = 5


class InvalidToken(Exception):
    """Telegram does not know this bot token."""


class BotInUseElsewhere(Exception):
    """Another client is already polling this bot (HTTP 409)."""


class TelegramUnreachable(Exception):
    """A network error, a 5xx, or a reply that is not valid JSON."""


class SendResult(enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"  # the chat is gone or the user blocked the bot
    RETRY = "retry"  # network error, 5xx or 429: try again later
    REJECTED = "rejected"  # other 4xx: retrying cannot help, drop the entry


class BotApi:
    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token
        # Set by `send` on every call: the 429 hint, else None.
        self.retry_after_s: int | None = None

    def __repr__(self) -> str:
        return "BotApi()"

    async def _call(
        self,
        http_method: str,
        api_method: str,
        payload: dict[str, Any] | None = None,
        timeout_s: float = _CALL_TIMEOUT_S,
    ) -> tuple[int, dict[str, Any]]:
        """Return (status, JSON body). Raise TelegramUnreachable on a network error."""
        url = f"{_API}/bot{self._token}/{api_method}"
        try:
            async with self._session.request(
                http_method,
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise TelegramUnreachable("Telegram request failed") from None
        if not isinstance(body, dict):
            body = {}
            if status < 400:
                raise TelegramUnreachable("Telegram sent an unreadable reply") from None
        return status, body

    async def _result(self, api_method: str, http_method: str = "GET", **kw: Any) -> Any:
        status, body = await self._call(http_method, api_method, **kw)
        if status in (401, 404):
            raise InvalidToken("Telegram rejected the bot token")
        if status == 409:
            raise BotInUseElsewhere("Another client is polling this bot")
        if status >= 400 or not body.get("ok"):
            raise TelegramUnreachable(f"Telegram answered HTTP {status}")
        return body.get("result")

    async def get_me(self) -> str:
        result = await self._result("getMe")
        username = result.get("username") if isinstance(result, dict) else None
        if not isinstance(username, str):
            raise TelegramUnreachable("Telegram sent an unreadable reply")
        return username

    async def webhook_url(self) -> str | None:
        result = await self._result("getWebhookInfo")
        url = result.get("url") if isinstance(result, dict) else None
        return url if isinstance(url, str) and url else None

    async def get_updates(self, offset: int | None, timeout_s: int = 25) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._result(
            "getUpdates",
            "POST",
            payload=payload,
            timeout_s=timeout_s + _POLL_MARGIN_S,
        )
        return result if isinstance(result, list) else []

    async def send(
        self, chat_id: int, html: str, button: tuple[str, str] | None
    ) -> SendResult:
        self.retry_after_s = None
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if button is not None:
            label, url = button
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}]]}
        try:
            status, body = await self._call("POST", "sendMessage", payload)
        except TelegramUnreachable:
            return SendResult.RETRY
        if status == 200 and body.get("ok"):
            return SendResult.OK
        if status == 429:
            hint = (body.get("parameters") or {}).get("retry_after")
            self.retry_after_s = hint if isinstance(hint, int) else None
            return SendResult.RETRY
        if status >= 500:
            return SendResult.RETRY
        if status == 403:
            return SendResult.BLOCKED
        if status == 400 and "chat not found" in str(body.get("description", "")).lower():
            return SendResult.BLOCKED
        if 400 <= status < 500:
            return SendResult.REJECTED
        return SendResult.RETRY
