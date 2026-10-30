"""Persistent state for island Telegram notifications.

One store holds the owner's bot token, the linked chats (one per paired phone),
one-time link codes, the getUpdates offset, the alert detector's state and the
send queue. The token never appears in `repr`, `str` or a log line.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
STORAGE_KEY = "svitgrid_telegram"
CODE_TTL = timedelta(minutes=10)
_SAVE_DELAY_S = 1
_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")


def parse_token(raw: str) -> str | None:
    """Return the first bot token found in `raw`, such as a BotFather message."""
    match = _TOKEN_RE.search(raw or "")
    return match.group(0) if match else None


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass
class Chat:
    """A Telegram chat linked to one paired phone."""

    device_id: str
    chat_id: int
    username: str | None
    enabled: bool
    language: str
    linked_at: str


class TelegramState:
    """Loads and saves the island Telegram state."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._reset()

    def _reset(self) -> None:
        self._token: str | None = None
        self._username: str | None = None
        self._chats: dict[str, Chat] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._offset = 0
        self._detector: dict[str, Any] = {}
        self._queue: list[dict[str, Any]] = []
        self._status: str | None = None

    def __repr__(self) -> str:
        return f"TelegramState(bot={self._username!r}, chats={len(self._chats)})"

    __str__ = __repr__

    def _data(self) -> dict[str, Any]:
        return {
            "token": self._token,
            "botUsername": self._username,
            "chats": [vars(c).copy() for c in self._chats.values()],
            "codes": self._codes,
            "offset": self._offset,
            "detector": self._detector,
            "queue": self._queue,
            "status": self._status,
        }

    def _save_later(self) -> None:
        self._store.async_delay_save(self._data, _SAVE_DELAY_S)

    async def _save_now(self) -> None:
        await self._store.async_save(self._data())

    async def flush(self) -> None:
        """Write any pending delayed save now (shutdown, tests)."""
        async with self._lock:
            await self._save_now()

    async def load(self) -> None:
        async with self._lock:
            self._reset()
            raw = await self._store.async_load()
            if not isinstance(raw, dict):
                return
            token = raw.get("token")
            self._token = token if isinstance(token, str) else None
            name = raw.get("botUsername")
            self._username = name if isinstance(name, str) else None
            for c in raw.get("chats") or []:
                try:
                    chat = Chat(
                        device_id=str(c["device_id"]),
                        chat_id=int(c["chat_id"]),
                        username=c.get("username"),
                        enabled=bool(c.get("enabled", True)),
                        language=str(c.get("language") or "en"),
                        linked_at=str(c.get("linked_at") or ""),
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                self._chats[chat.device_id] = chat
            codes = raw.get("codes")
            self._codes = codes if isinstance(codes, dict) else {}
            off = raw.get("offset")
            self._offset = off if isinstance(off, int) else 0
            det = raw.get("detector")
            self._detector = det if isinstance(det, dict) else {}
            q = raw.get("queue")
            self._queue = q if isinstance(q, list) else []
            st = raw.get("status")
            self._status = st if isinstance(st, str) else None

    # Bot

    async def set_bot(self, token: str, username: str) -> None:
        async with self._lock:
            self._reset()
            self._token = token
            self._username = username
            await self._save_now()

    async def clear(self) -> None:
        async with self._lock:
            self._reset()
            await self._save_now()

    def token(self) -> str | None:
        return self._token

    def bot_username(self) -> str | None:
        return self._username

    @property
    def status(self) -> str | None:
        return self._status

    async def set_status(self, status: str | None) -> None:
        async with self._lock:
            self._status = status
            self._save_later()

    # Chats

    def chat_for(self, device_id: str) -> Chat | None:
        return self._chats.get(device_id)

    def chats(self) -> list[Chat]:
        return list(self._chats.values())

    async def set_enabled(self, device_id: str, enabled: bool) -> None:
        async with self._lock:
            chat = self._chats.get(device_id)
            if chat is not None:
                chat.enabled = enabled
                self._save_later()

    async def unlink(self, device_id: str) -> None:
        async with self._lock:
            if self._chats.pop(device_id, None) is not None:
                self._save_later()

    async def unlink_chat_id(self, chat_id: int) -> None:
        async with self._lock:
            gone = [d for d, c in self._chats.items() if c.chat_id == chat_id]
            for d in gone:
                del self._chats[d]
            if gone:
                self._save_later()

    # Link codes

    def _prune(self, now: datetime) -> None:
        self._codes = {h: c for h, c in self._codes.items() if _expires(c) > now}

    async def issue_code(self, device_id: str, language: str, now: datetime) -> str:
        async with self._lock:
            self._prune(now)
            self._codes = {h: c for h, c in self._codes.items() if c.get("device_id") != device_id}
            code = secrets.token_urlsafe(12)
            self._codes[_hash(code)] = {
                "device_id": device_id,
                "language": language,
                "expiresAt": (now + CODE_TTL).isoformat(),
            }
            self._save_later()
            return code

    async def claim_code(
        self, code: str, chat_id: int, username: str | None, now: datetime
    ) -> Chat | None:
        async with self._lock:
            entry = self._codes.pop(_hash(code), None)
            self._prune(now)
            self._save_later()
            if entry is None or _expires(entry) <= now:
                return None
            device_id = entry["device_id"]
            for d in [d for d, c in self._chats.items() if c.chat_id == chat_id]:
                del self._chats[d]
            chat = Chat(
                device_id=device_id,
                chat_id=chat_id,
                username=username,
                enabled=True,
                language=entry.get("language") or "en",
                linked_at=now.isoformat(),
            )
            self._chats[device_id] = chat
            return chat

    def has_pending_codes(self, now: datetime) -> bool:
        return any(_expires(c) > now for c in self._codes.values())

    # Polling offset, detector, queue

    @property
    def offset(self) -> int:
        return self._offset

    async def set_offset(self, offset: int) -> None:
        async with self._lock:
            self._offset = offset
            self._save_later()

    def detector(self) -> dict[str, Any]:
        return self._detector

    async def set_detector(self, detector: dict[str, Any]) -> None:
        async with self._lock:
            self._detector = detector
            self._save_later()

    def queue(self) -> list[dict[str, Any]]:
        return self._queue

    async def set_queue(self, queue: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._queue = queue
            self._save_later()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _expires(entry: dict[str, Any]) -> datetime:
    try:
        return _parse(entry["expiresAt"])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
