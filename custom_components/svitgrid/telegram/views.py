"""LAN routes for island Telegram notifications.

The Svitgrid app calls these to set the owner's bot, make a link code, switch a
person's chat on or off, unlink it and send a test. The phone station serves the
same JSON. Per-person routes take the calling phone's island key; the two bot
routes also need a trusted signature, since they change the station for everyone.
A station with no bot token makes no outbound call on any route but `PUT /bot`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import DOMAIN
from ..island_lan import _cloud_copy_of, _json_error, signed_lan_request
from . import copy
from .bot_api import BotApi, InvalidToken, SendResult, TelegramUnreachable
from .state import CODE_TTL, TelegramState, parse_token

_LOGGER = logging.getLogger(__name__)

ACTION_SET_BOT = "telegram_set_bot"
ACTION_REMOVE_BOT = "telegram_remove_bot"
OPEN_URL = "https://svitgrid.app/open?route=/"


async def telegram_state(hass) -> TelegramState:
    """The one `TelegramState`, created and loaded on first use."""
    domain = hass.data.setdefault(DOMAIN, {})
    state = domain.get("telegram_state")
    if state is None:
        state = TelegramState(hass)
        await state.load()
        domain["telegram_state"] = state
    domain.setdefault("telegram_wake", lambda: None)
    return state


async def unlink_device(hass, device_id: str | None) -> None:
    """Drop the chat of a device whose island access was revoked."""
    state = hass.data.get(DOMAIN, {}).get("telegram_state")
    if state is not None and device_id:
        await state.unlink(device_id)


def _json(body: dict[str, Any], status: int = 200) -> web.Response:
    return web.Response(status=status, text=json.dumps(body), content_type="application/json")


async def _caller(request) -> tuple[str | None, web.Response | None]:
    """The device id behind `X-Island-Key`, or a 401."""
    hass = request.app["hass"]
    keystore = hass.data.get(DOMAIN, {}).get("keystore")
    state = await keystore.load() if keystore is not None else None
    key = request.headers.get("X-Island-Key")
    device_id = state.device_for_island_key(key) if state is not None and key else None
    if device_id is None:
        return None, _json_error(401, "unauthorized")
    return device_id, None


def _status_body(hass, state: TelegramState, device_id: str | None) -> dict[str, Any]:
    configured = state.token() is not None
    me = None
    if configured and device_id is not None:
        chat = state.chat_for(device_id)
        me = {
            "linked": chat is not None,
            "enabled": bool(chat and chat.enabled),
            "username": chat.username if chat else None,
        }
    entries = hass.config_entries.async_entries(DOMAIN)
    return {
        "botConfigured": configured,
        "botUsername": state.bot_username(),
        "status": state.status,
        "me": me,
        "chats": len(state.chats()),
        "sends": any(not _cloud_copy_of(e) for e in entries),
    }


async def _body_dict(request) -> dict | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _station_title(hass) -> str:
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0].title if entries and entries[0].title else "Svitgrid"


class SvitgridTelegramView(HomeAssistantView):
    """GET /api/svitgrid/telegram — bot and calling-phone status."""

    url = "/api/svitgrid/telegram"
    name = "api:svitgrid:telegram"
    requires_auth = False

    async def get(self, request) -> web.Response:  # noqa: D102
        device_id, error = await _caller(request)
        if error is not None:
            return error
        hass = request.app["hass"]
        return _json(_status_body(hass, await telegram_state(hass), device_id))


class SvitgridTelegramBotView(HomeAssistantView):
    """PUT / DELETE /api/svitgrid/telegram/bot — set or remove the owner's bot."""

    url = "/api/svitgrid/telegram/bot"
    name = "api:svitgrid:telegram:bot"
    requires_auth = False

    async def put(self, request) -> web.Response:  # noqa: D102
        signed, error = await signed_lan_request(request, ACTION_SET_BOT)
        if error is not None:
            return error
        hass = request.app["hass"]
        raw = signed.get("token")
        token = parse_token(raw) if isinstance(raw, str) else None
        if token is None:
            return _json_error(400, "invalid_token")
        api = BotApi(async_get_clientsession(hass), token)
        try:
            username = await api.get_me()
            webhook = await api.webhook_url()
        except InvalidToken:
            return _json_error(400, "invalid_token")
        except TelegramUnreachable:
            return _json_error(502, "telegram_unreachable")
        if webhook:
            return _json_error(409, "bot_has_webhook")
        state = await telegram_state(hass)
        await state.set_bot(token, username)
        device_id, _ = await _caller(request)
        return _json(_status_body(hass, state, device_id))

    async def delete(self, request) -> web.Response:  # noqa: D102
        _, error = await signed_lan_request(request, ACTION_REMOVE_BOT)
        if error is not None:
            return error
        hass = request.app["hass"]
        state = await telegram_state(hass)
        await state.clear()
        device_id, _ = await _caller(request)
        return _json(_status_body(hass, state, device_id))


class SvitgridTelegramLinkView(HomeAssistantView):
    """POST /api/svitgrid/telegram/link — a one-time code for the caller's chat."""

    url = "/api/svitgrid/telegram/link"
    name = "api:svitgrid:telegram:link"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        device_id, error = await _caller(request)
        if error is not None:
            return error
        body = await _body_dict(request)
        if body is None:
            return _json_error(400, "bad_request")
        language = body.get("language")
        if not isinstance(language, str) or not language:
            language = "en"
        hass = request.app["hass"]
        state = await telegram_state(hass)
        username = state.bot_username()
        if state.token() is None or not username:
            return _json_error(409, "no_bot")
        now = datetime.now(UTC)
        code = await state.issue_code(device_id, language, now)
        hass.data[DOMAIN]["telegram_wake"]()
        return _json(
            {
                "url": f"https://t.me/{username}?start={code}",
                "expiresAt": (now + CODE_TTL).isoformat(),
            }
        )


class SvitgridTelegramMeView(HomeAssistantView):
    """PATCH / DELETE /api/svitgrid/telegram/me — the caller's own chat."""

    url = "/api/svitgrid/telegram/me"
    name = "api:svitgrid:telegram:me"
    requires_auth = False

    async def patch(self, request) -> web.Response:  # noqa: D102
        device_id, error = await _caller(request)
        if error is not None:
            return error
        body = await _body_dict(request)
        if body is None or not isinstance(body.get("enabled"), bool):
            return _json_error(400, "bad_request")
        hass = request.app["hass"]
        state = await telegram_state(hass)
        await state.set_enabled(device_id, body["enabled"])
        return _json(_status_body(hass, state, device_id))

    async def delete(self, request) -> web.Response:  # noqa: D102
        device_id, error = await _caller(request)
        if error is not None:
            return error
        hass = request.app["hass"]
        state = await telegram_state(hass)
        await state.unlink(device_id)
        return _json(_status_body(hass, state, device_id))


class SvitgridTelegramTestView(HomeAssistantView):
    """POST /api/svitgrid/telegram/test — a test message to the caller's chat."""

    url = "/api/svitgrid/telegram/test"
    name = "api:svitgrid:telegram:test"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        device_id, error = await _caller(request)
        if error is not None:
            return error
        hass = request.app["hass"]
        state = await telegram_state(hass)
        token = state.token()
        chat = state.chat_for(device_id)
        if token is None or chat is None:
            return _json_error(409, "not_linked")
        html = copy.render("test", chat.language, station=_station_title(hass))
        button = (copy.open_button(chat.language), OPEN_URL)
        api = BotApi(async_get_clientsession(hass), token)
        try:
            result = await api.send(chat.chat_id, html, button)
        except InvalidToken:
            await state.set_status("invalid_token")
            return _json_error(400, "invalid_token")
        except TelegramUnreachable:
            return _json_error(502, "telegram_unreachable")
        if result is SendResult.BLOCKED:
            await state.unlink(device_id)
            return _json_error(409, "not_linked")
        if result is not SendResult.OK:
            return _json_error(502, "telegram_unreachable")
        return _json({"ok": True})


TELEGRAM_VIEWS = (
    SvitgridTelegramView,
    SvitgridTelegramBotView,
    SvitgridTelegramLinkView,
    SvitgridTelegramMeView,
    SvitgridTelegramTestView,
)


async def async_ensure_telegram_views(hass) -> None:
    """Register the routes once, as `ensure_hello_view` does, and load the state.

    Routes are global to `hass.http` and outlive a config-entry reload, so a
    second registration is swallowed. Never raises.
    """
    try:
        await telegram_state(hass)
    except Exception:  # noqa: BLE001
        # A corrupt Telegram store must not take the integration down. The
        # routes build an empty state on first use.
        _LOGGER.exception("Could not load the Telegram state")
        hass.data.setdefault(DOMAIN, {})["telegram_state"] = TelegramState(hass)
        hass.data[DOMAIN].setdefault("telegram_wake", lambda: None)
    http = getattr(hass, "http", None)
    if http is None:
        return
    for view_cls in TELEGRAM_VIEWS:
        try:
            http.register_view(view_cls())
        except RuntimeError as err:
            _LOGGER.debug("%s already registered: %s", view_cls.__name__, err)
