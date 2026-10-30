"""The island Telegram runtime: alert queue, sender and link poller.

`on_reading` feeds the detector and writes one queue entry per alert and
enabled chat before anything is sent, so an alert survives a restart or a
Telegram outage. One sender task drains the queue oldest first and waits 60 s
after a failed send. One poller task long-polls `getUpdates`, but only while a
link code is pending, and links the chat that sends `/start <code>`.

Nothing here logs the token, a message text or a chat id above debug level.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ..const import DOMAIN
from . import copy
from .bot_api import BotApi, BotInUseElsewhere, InvalidToken, SendResult, TelegramUnreachable
from .detector import evaluate
from .state import TelegramState

_LOGGER = logging.getLogger(__name__)

RETRY_S = 60
POLL_TIMEOUT_S = 25
POLL_ERROR_S = 5
MAX_QUEUE = 50
MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000

OPEN_URL = "https://svitgrid.app/open?route=/"
ALERT_KINDS = ("outage", "grid_restored")

STATUS_INVALID_TOKEN = "invalid_token"
STATUS_IN_USE = "bot_in_use_elsewhere"

_START_RE = re.compile(r"/start(?:@[A-Za-z0-9_]+)?\s+(\S+)")


class TestOutcome(enum.Enum):
    """What `send_test` did. The /test route maps each to a status code."""

    SENT = "sent"
    NOT_LINKED = "not_linked"
    INVALID_TOKEN = "invalid_token"
    UNREACHABLE = "unreachable"


def station_title(hass) -> str:
    """The first entry's title, or "Svitgrid"."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0].title if entries and entries[0].title else "Svitgrid"


def _noop() -> None:
    return None


def _int(value: Any) -> int:
    return value if type(value) is int else 0


def _order(entry: dict[str, Any]) -> tuple[int, int]:
    """Send order: the event time, then the time the entry was queued."""
    return (_int(entry.get("eventAtMs")), _int(entry.get("createdAtMs")))


def _trim(queue: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Drop entries queued over 7 days ago and keep the newest 50, in send order."""
    fresh = [
        e
        for e in queue
        if isinstance(e, dict) and _int(e.get("createdAtMs")) >= now_ms - MAX_AGE_MS
    ]
    return sorted(fresh, key=_order)[-MAX_QUEUE:]


class TelegramRuntime:
    """Sends queued alerts and links chats for one install."""

    def __init__(
        self,
        hass,
        state: TelegramState,
        session_factory: Callable[[], Any],
        now: Callable[[], datetime],
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        api_factory: Callable[[Any, str], BotApi] = BotApi,
    ) -> None:
        self._hass = hass
        self._state = state
        self._session_factory = session_factory
        self._now = now
        self._sleep = sleep
        self._api_factory = api_factory
        # Guards every read-modify-write of the queue and the detector state.
        self._lock = asyncio.Lock()
        self._sender: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._running = False

    # Lifecycle

    async def start(self) -> None:
        self._running = True
        self._hass.data.setdefault(DOMAIN, {})["telegram_wake"] = self.wake
        self._kick_sender()
        self.wake()

    async def stop(self) -> None:
        self._running = False
        domain = self._hass.data.get(DOMAIN)
        if domain is not None and domain.get("telegram_wake") == self.wake:
            domain["telegram_wake"] = _noop
        for task in (self._sender, self._poller):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._sender = None
        self._poller = None

    def wake(self) -> None:
        """Start the link poller if a code is pending, and retry the queue."""
        if not self._running:
            return
        self._kick_sender()
        if self._poller is not None and not self._poller.done():
            return
        if not self._can_poll():
            return
        self._poller = self._hass.async_create_background_task(
            self._poll_loop(), "svitgrid_telegram_poller"
        )

    # Helpers

    def _now_ms(self) -> int:
        return int(self._now().timestamp() * 1000)

    def _api(self) -> BotApi | None:
        token = self._state.token()
        return self._api_factory(self._session_factory(), token) if token else None

    def _can_send(self) -> bool:
        return self._state.token() is not None and self._state.status != STATUS_INVALID_TOKEN

    def _can_poll(self) -> bool:
        return self._can_send() and self._state.has_pending_codes(self._now())

    def _station(self, household_id: str | None) -> str:
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            if household_id and (
                entry.data.get("household_id") == household_id or entry.entry_id == household_id
            ):
                if entry.title:
                    return entry.title
                break
        return station_title(self._hass)

    # Readings

    async def on_reading(self, household_id: str, inverter_id: str, reading: dict) -> None:
        """Run the detector on one reading and queue any alert for every enabled chat.

        With no bot or no enabled chat the detector does not run, so an owner
        who sets up during an outage first hears of the next outage, never a
        lone restore.
        """
        now_ms = self._now_ms()
        async with self._lock:
            chats = [c for c in self._state.chats() if c.enabled]
            if self._state.token() is None or not chats:
                return
            detector, alerts = evaluate(
                self._state.detector(), household_id, inverter_id, reading, now_ms
            )
            await self._state.set_detector(detector)
            added = [
                {
                    "id": uuid.uuid4().hex,
                    "chatDeviceId": chat.device_id,
                    "kind": alert.type,
                    "householdId": alert.household_id,
                    "eventAtMs": alert.event_at_ms,
                    "soc": alert.battery_soc,
                    "createdAtMs": now_ms,
                }
                for alert in alerts
                for chat in chats
            ]
            if not added:
                return
            await self._state.set_queue(_trim([*self._state.queue(), *added], now_ms))
        self._kick_sender()

    # Sender

    def _kick_sender(self) -> None:
        if not self._running:
            return
        if self._sender is not None and not self._sender.done():
            return
        if not self._can_send() or not self._state.queue():
            return
        self._sender = self._hass.async_create_background_task(
            self._send_loop(), "svitgrid_telegram_sender"
        )

    async def _drop(self, field: str, value: str) -> None:
        """Remove every queue entry whose `field` equals `value`."""
        async with self._lock:
            await self._state.set_queue([e for e in self._state.queue() if e.get(field) != value])

    async def _next_entry(self) -> dict[str, Any] | None:
        """Trim the queue and return its first entry in send order.

        Drops entries that are malformed or whose chat is gone or switched off.
        """
        async with self._lock:
            queue = _trim(self._state.queue(), self._now_ms())
            live = []
            for entry in queue:
                chat = self._state.chat_for(entry.get("chatDeviceId"))
                if (
                    isinstance(entry.get("id"), str)
                    and entry.get("kind") in ALERT_KINDS
                    and chat is not None
                    and chat.enabled
                ):
                    live.append(entry)
            if live != self._state.queue():
                await self._state.set_queue(live)
            return live[0] if live else None

    def _render(self, entry: dict[str, Any], language: str) -> str:
        event_ms = entry.get("eventAtMs")
        event_at = (
            datetime.fromtimestamp(event_ms / 1000, UTC) if isinstance(event_ms, int) else None
        )
        return copy.render(
            entry.get("kind"),
            language,
            station=self._station(entry.get("householdId")),
            soc=entry.get("soc"),
            event_at=event_at,
            tz=self._hass.config.time_zone,
            now=self._now(),
        )

    async def _send_loop(self) -> None:
        while self._can_send():
            try:
                if not await self._send_next():
                    return
            except Exception:  # noqa: BLE001
                # A storage error must not end the sender for good.
                _LOGGER.exception("The Telegram alert sender failed; retrying")
                await self._sleep(RETRY_S)

    async def _send_next(self) -> bool:
        """Send the next entry. False when the sender should stop."""
        entry = await self._next_entry()
        if entry is None:
            return False
        chat = self._state.chat_for(entry["chatDeviceId"])
        api = self._api()
        if chat is None or api is None:
            return True
        button = (copy.open_button(chat.language), OPEN_URL)
        try:
            result = await api.send(chat.chat_id, self._render(entry, chat.language), button)
        except InvalidToken:
            _LOGGER.warning("Telegram rejected the bot token; alerts wait for a new bot")
            await self._state.set_status(STATUS_INVALID_TOKEN)
            return False
        except Exception:  # noqa: BLE001
            # Not a Telegram answer, so retrying the same entry cannot help.
            _LOGGER.exception("A queued %s alert could not be sent; it is dropped", entry["kind"])
            result = SendResult.REJECTED
        if result is SendResult.OK:
            await self._drop("id", entry["id"])
        elif result is SendResult.BLOCKED:
            device_id = entry["chatDeviceId"]
            _LOGGER.info("A Telegram chat blocked the bot; it is unlinked")
            await self._state.unlink(device_id)
            await self._drop("chatDeviceId", device_id)
        elif result is SendResult.REJECTED:
            _LOGGER.warning("Telegram rejected a queued %s alert; it is dropped", entry["kind"])
            await self._drop("id", entry["id"])
        else:
            await self._sleep(getattr(api, "retry_after_s", None) or RETRY_S)
        return True

    async def send_test(self, device_id: str) -> TestOutcome:
        """Send the test message to one device's chat."""
        chat = self._state.chat_for(device_id)
        api = self._api()
        if chat is None or api is None:
            return TestOutcome.NOT_LINKED
        html = copy.render("test", chat.language, station=station_title(self._hass))
        button = (copy.open_button(chat.language), OPEN_URL)
        try:
            result = await api.send(chat.chat_id, html, button)
        except InvalidToken:
            await self._state.set_status(STATUS_INVALID_TOKEN)
            return TestOutcome.INVALID_TOKEN
        if result is SendResult.OK:
            return TestOutcome.SENT
        if result is SendResult.BLOCKED:
            await self._state.unlink(device_id)
            return TestOutcome.NOT_LINKED
        return TestOutcome.UNREACHABLE

    # Link poller

    async def _poll_loop(self) -> None:
        while self._can_poll():
            try:
                if not await self._poll_once():
                    return
            except Exception:  # noqa: BLE001
                # A storage error must not end the poller for good.
                _LOGGER.exception("The Telegram link poller failed; retrying")
                await self._sleep(POLL_ERROR_S)

    async def _poll_once(self) -> bool:
        """One long poll. False when the poller should stop."""
        api = self._api()
        if api is None:
            return False
        try:
            updates = await api.get_updates(self._state.offset or None, POLL_TIMEOUT_S)
        except BotInUseElsewhere:
            _LOGGER.warning("Another client polls this Telegram bot; linking paused")
            await self._state.set_status(STATUS_IN_USE)
            return False
        except InvalidToken:
            _LOGGER.warning("Telegram rejected the bot token; linking stopped")
            await self._state.set_status(STATUS_INVALID_TOKEN)
            return False
        except TelegramUnreachable:
            await self._sleep(POLL_ERROR_S)
            return True
        if self._state.status == STATUS_IN_USE:
            await self._state.set_status(None)
        await self._handle_updates(api, updates)
        return True

    async def _handle_updates(self, api: BotApi, updates: list[dict]) -> None:
        ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int)]
        if ids:
            await self._state.set_offset(max(ids) + 1)
        for update in updates:
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            text = message.get("text")
            chat_id = (message.get("chat") or {}).get("id")
            if not isinstance(text, str) or not isinstance(chat_id, int):
                continue
            match = _START_RE.fullmatch(text.strip())
            if match is None:
                continue
            username = (message.get("from") or {}).get("username")
            chat = await self._state.claim_code(
                match.group(1),
                chat_id,
                username if isinstance(username, str) else None,
                self._now(),
            )
            if chat is None:
                _LOGGER.debug("A /start with an unknown or expired code was ignored")
                continue
            _LOGGER.info("A Telegram chat was linked")
            html = copy.render("linked", chat.language, station=station_title(self._hass))
            try:
                await api.send(chat_id, html, None)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("The Telegram welcome message was not sent", exc_info=True)
