"""The Telegram Bot API client: results map to what the alert queue does next."""

import json

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.svitgrid.telegram.bot_api import (
    BotApi,
    BotInUseElsewhere,
    InvalidToken,
    SendResult,
    TelegramUnreachable,
)

TOKEN = "123456:SECRET-token_abc"
BASE = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture
async def api(hass, aioclient_mock):
    return BotApi(async_get_clientsession(hass), TOKEN)


async def test_get_me_returns_username(api, aioclient_mock):
    aioclient_mock.get(f"{BASE}/getMe", json={"ok": True, "result": {"username": "my_bot"}})
    assert await api.get_me() == "my_bot"


@pytest.mark.parametrize("status", [401, 404])
async def test_get_me_invalid_token(api, aioclient_mock, status):
    aioclient_mock.get(f"{BASE}/getMe", status=status, json={"ok": False})
    with pytest.raises(InvalidToken):
        await api.get_me()


async def test_get_me_network_error_is_unreachable(api, aioclient_mock):
    aioclient_mock.get(f"{BASE}/getMe", exc=aiohttp.ClientError(f"boom {BASE}/getMe"))
    with pytest.raises(TelegramUnreachable):
        await api.get_me()


async def test_get_me_5xx_and_malformed_json_are_unreachable(api, aioclient_mock):
    aioclient_mock.get(f"{BASE}/getMe", status=502, text="bad gateway")
    with pytest.raises(TelegramUnreachable):
        await api.get_me()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/getMe", status=200, text="not json")
    with pytest.raises(TelegramUnreachable):
        await api.get_me()


async def test_webhook_url(api, aioclient_mock):
    aioclient_mock.get(
        f"{BASE}/getWebhookInfo", json={"ok": True, "result": {"url": "https://x.example/h"}}
    )
    assert await api.webhook_url() == "https://x.example/h"


async def test_webhook_url_empty_is_none(api, aioclient_mock):
    aioclient_mock.get(f"{BASE}/getWebhookInfo", json={"ok": True, "result": {"url": ""}})
    assert await api.webhook_url() is None


async def test_get_updates_returns_result_and_sends_params(api, aioclient_mock):
    updates = [{"update_id": 5, "message": {"text": "/start abc"}}]
    aioclient_mock.post(f"{BASE}/getUpdates", json={"ok": True, "result": updates})
    assert await api.get_updates(offset=5, timeout_s=10) == updates
    body = aioclient_mock.mock_calls[0][2]
    assert body["offset"] == 5
    assert body["timeout"] == 10
    assert body["allowed_updates"] == ["message"]


async def test_get_updates_conflict(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/getUpdates", status=409, json={"ok": False})
    with pytest.raises(BotInUseElsewhere):
        await api.get_updates(offset=None)


async def test_get_updates_network_error(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/getUpdates", exc=TimeoutError())
    with pytest.raises(TelegramUnreachable):
        await api.get_updates(offset=None)


async def test_send_ok_with_button(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/sendMessage", json={"ok": True, "result": {}})
    result = await api.send(42, "<b>hi</b>", ("Open", "https://svitgrid.app/x"))
    assert result is SendResult.OK
    body = aioclient_mock.mock_calls[0][2]
    assert body["chat_id"] == 42
    assert body["text"] == "<b>hi</b>"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    assert body["reply_markup"] == {
        "inline_keyboard": [[{"text": "Open", "url": "https://svitgrid.app/x"}]]
    }


async def test_send_without_button_has_no_reply_markup(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/sendMessage", json={"ok": True, "result": {}})
    assert await api.send(42, "hi", None) is SendResult.OK
    assert "reply_markup" not in aioclient_mock.mock_calls[0][2]


async def test_send_403_is_blocked(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/sendMessage", status=403, json={"ok": False})
    assert await api.send(42, "hi", None) is SendResult.BLOCKED


async def test_send_400_chat_not_found_is_blocked(api, aioclient_mock):
    aioclient_mock.post(
        f"{BASE}/sendMessage",
        status=400,
        json={"ok": False, "description": "Bad Request: chat not found"},
    )
    assert await api.send(42, "hi", None) is SendResult.BLOCKED


async def test_send_other_400_is_rejected(api, aioclient_mock):
    aioclient_mock.post(
        f"{BASE}/sendMessage",
        status=400,
        json={"ok": False, "description": "Bad Request: can't parse entities"},
    )
    assert await api.send(42, "<b>", None) is SendResult.REJECTED


async def test_send_429_retries_with_retry_after(api, aioclient_mock):
    aioclient_mock.post(
        f"{BASE}/sendMessage",
        status=429,
        json={"ok": False, "parameters": {"retry_after": 7}},
    )
    assert await api.send(42, "hi", None) is SendResult.RETRY
    assert api.retry_after_s == 7


async def test_send_retry_after_resets(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/sendMessage", status=429, json={"parameters": {"retry_after": 7}})
    await api.send(42, "hi", None)
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/sendMessage", status=500, text="oops")
    assert await api.send(42, "hi", None) is SendResult.RETRY
    assert api.retry_after_s is None


async def test_send_5xx_and_network_error_retry(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/sendMessage", status=503, text="down")
    assert await api.send(42, "hi", None) is SendResult.RETRY
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/sendMessage", exc=aiohttp.ClientError(BASE))
    assert await api.send(42, "hi", None) is SendResult.RETRY


async def test_token_never_in_exception_text(api, aioclient_mock):
    aioclient_mock.get(f"{BASE}/getMe", exc=aiohttp.ClientError(f"cannot reach {BASE}/getMe"))
    with pytest.raises(TelegramUnreachable) as err:
        await api.get_me()
    for text in (str(err.value), repr(err.value), repr(api), str(api)):
        assert TOKEN not in text
        assert "SECRET" not in text
        assert "api.telegram.org" not in text
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/getMe", status=401, json={})
    with pytest.raises(InvalidToken) as err2:
        await api.get_me()
    assert TOKEN not in json.dumps(str(err2.value))


@pytest.mark.parametrize("status", [401, 404])
async def test_send_invalid_token_raises(api, aioclient_mock, status):
    aioclient_mock.post(f"{BASE}/sendMessage", status=status, json={"ok": False})
    with pytest.raises(InvalidToken) as err:
        await api.send(42, "hi", None)
    assert TOKEN not in str(err.value)
    assert TOKEN not in repr(err.value)


async def test_send_429_without_parameters_has_no_retry_after(api, aioclient_mock):
    aioclient_mock.post(f"{BASE}/sendMessage", status=429, json={"ok": False})
    assert await api.send(42, "hi", None) is SendResult.RETRY
    assert api.retry_after_s is None


@pytest.mark.parametrize("hint", [True, 0, -3])
async def test_send_429_ignores_bad_retry_after(api, aioclient_mock, hint):
    aioclient_mock.post(
        f"{BASE}/sendMessage", status=429, json={"parameters": {"retry_after": hint}}
    )
    assert await api.send(42, "hi", None) is SendResult.RETRY
    assert api.retry_after_s is None
