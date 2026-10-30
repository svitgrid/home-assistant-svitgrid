"""The island Telegram routes: auth, errors, and the token never leaving."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.island_lan import SvitgridKeysRevokeView
from custom_components.svitgrid.telegram import views as tv
from custom_components.svitgrid.telegram.state import Chat
from tests.test_island_lan import (
    ISLAND_KEY,
    NOW_MS,
    OTHER_ISLAND_KEY,
    _Admin,
    _body,
    _keystore,
    _Req,
)

TOKEN = "123456:" + "A" * 35
BASE = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture(autouse=True)
def _pinned_clock():
    from unittest.mock import patch

    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


async def _setup(hass, *, token: bool = False):
    admin = _Admin()
    other = _Admin()
    ks = await _keystore(hass, admin, other)
    await ks.async_add_island_key("phone-2", OTHER_ISLAND_KEY, label="Other")
    state = await tv.telegram_state(hass)
    if token:
        await state.set_bot(TOKEN, "my_bot")
    return admin, other, ks, state


def _link(state, device_id="phone-1", chat_id=77):
    state._chats[device_id] = Chat(
        device_id, chat_id, "ann", True, "uk", "2026-10-30T00:00:00+00:00"
    )


async def test_get_without_island_key_is_401(hass):
    await _setup(hass)
    resp = await tv.SvitgridTelegramView().get(_Req(hass, island_key=None))
    assert resp.status == 401


async def test_get_unknown_island_key_is_401(hass):
    await _setup(hass)
    resp = await tv.SvitgridTelegramView().get(_Req(hass, island_key="z" * 43))
    assert resp.status == 401


async def test_get_shape_and_me(hass):
    _, _, _, state = await _setup(hass, token=True)
    _link(state)
    body = _body(await tv.SvitgridTelegramView().get(_Req(hass)))
    assert body == {
        "botConfigured": True,
        "botUsername": "my_bot",
        "status": None,
        "me": {"linked": True, "enabled": True, "username": "ann"},
        "chats": 1,
        "sends": False,
    }
    other = _body(await tv.SvitgridTelegramView().get(_Req(hass, island_key=OTHER_ISLAND_KEY)))
    assert other["me"] == {"linked": False, "enabled": False, "username": None}


async def test_get_without_bot_has_null_me(hass):
    await _setup(hass)
    body = _body(await tv.SvitgridTelegramView().get(_Req(hass)))
    assert body["botConfigured"] is False
    assert body["me"] is None
    assert body["chats"] == 0


async def test_sends_true_when_a_cloud_copy_is_off(hass):
    await _setup(hass)
    MockConfigEntry(domain=DOMAIN, data={"cloud_ingest_enabled": False}).add_to_hass(hass)
    body = _body(await tv.SvitgridTelegramView().get(_Req(hass)))
    assert body["sends"] is True


async def test_station_without_token_makes_no_http_call(hass, aioclient_mock):
    await _setup(hass)
    assert (await tv.SvitgridTelegramView().get(_Req(hass))).status == 200
    assert (await tv.SvitgridTelegramLinkView().post(_Req(hass, {"language": "uk"}))).status == 409
    assert (await tv.SvitgridTelegramTestView().post(_Req(hass))).status == 409
    assert (await tv.SvitgridTelegramMeView().patch(_Req(hass, {"enabled": True}))).status == 200
    assert (await tv.SvitgridTelegramMeView().delete(_Req(hass))).status == 200
    assert aioclient_mock.call_count == 0


async def test_link_without_bot_is_no_bot(hass):
    await _setup(hass)
    resp = await tv.SvitgridTelegramLinkView().post(_Req(hass, {"language": "uk"}))
    assert resp.status == 409
    assert _body(resp)["error"] == "no_bot"


async def test_link_returns_url_and_wakes(hass):
    _, _, _, state = await _setup(hass, token=True)
    woken = []
    hass.data[DOMAIN]["telegram_wake"] = lambda: woken.append(1)
    resp = await tv.SvitgridTelegramLinkView().post(_Req(hass, {"language": "uk"}))
    body = _body(resp)
    assert resp.status == 200
    assert body["url"].startswith("https://t.me/my_bot?start=")
    datetime.fromisoformat(body["expiresAt"])
    assert woken == [1]
    assert state.has_pending_codes(datetime.now(UTC))


async def test_link_rejects_bad_body(hass):
    await _setup(hass, token=True)
    resp = await tv.SvitgridTelegramLinkView().post(_Req(hass, [1]))
    assert resp.status == 400


async def test_patch_and_delete_me(hass):
    _, _, _, state = await _setup(hass, token=True)
    _link(state)
    assert (await tv.SvitgridTelegramMeView().patch(_Req(hass, {"enabled": False}))).status == 200
    assert state.chat_for("phone-1").enabled is False
    assert (await tv.SvitgridTelegramMeView().patch(_Req(hass, {"enabled": "x"}))).status == 400
    assert (await tv.SvitgridTelegramMeView().delete(_Req(hass))).status == 200
    assert state.chat_for("phone-1") is None


async def test_put_bot_unsigned_is_401_or_403(hass):
    await _setup(hass)
    resp = await tv.SvitgridTelegramBotView().put(_Req(hass, {"token": TOKEN}, island_key=None))
    assert resp.status in (401, 403)
    assert (_body(resp)["error"]) == "unauthorized"
    resp = await tv.SvitgridTelegramBotView().put(_Req(hass, {"token": TOKEN}))
    assert (resp.status, _body(resp)["error"]) == (400, "bad_request")


async def test_put_bot_stores_token(hass, aioclient_mock):
    admin, _, _, state = await _setup(hass)
    aioclient_mock.get(f"{BASE}/getMe", json={"ok": True, "result": {"username": "my_bot"}})
    aioclient_mock.get(f"{BASE}/getWebhookInfo", json={"ok": True, "result": {"url": ""}})
    req = _Req(hass, admin.request("telegram_set_bot", token=f"use {TOKEN} please"))
    resp = await tv.SvitgridTelegramBotView().put(req)
    assert resp.status == 200
    assert _body(resp)["botUsername"] == "my_bot"
    assert state.token() == TOKEN
    assert TOKEN not in resp.text


async def test_put_bot_rejects_garbage_token(hass):
    admin, _, _, _ = await _setup(hass)
    resp = await tv.SvitgridTelegramBotView().put(
        _Req(hass, admin.request("telegram_set_bot", token="nope"))
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "invalid_token"


async def test_put_bot_rejects_webhook_bot(hass, aioclient_mock):
    admin, _, _, state = await _setup(hass)
    aioclient_mock.get(f"{BASE}/getMe", json={"ok": True, "result": {"username": "my_bot"}})
    aioclient_mock.get(
        f"{BASE}/getWebhookInfo", json={"ok": True, "result": {"url": "https://x.example/hook"}}
    )
    resp = await tv.SvitgridTelegramBotView().put(
        _Req(hass, admin.request("telegram_set_bot", token=TOKEN))
    )
    assert resp.status == 409
    assert _body(resp)["error"] == "bot_has_webhook"
    assert state.token() is None


async def test_put_bot_invalid_and_unreachable(hass, aioclient_mock):
    admin, _, _, _ = await _setup(hass)
    aioclient_mock.get(f"{BASE}/getMe", status=401, json={"ok": False})
    resp = await tv.SvitgridTelegramBotView().put(
        _Req(hass, admin.request("telegram_set_bot", token=TOKEN))
    )
    assert (resp.status, _body(resp)["error"]) == (400, "invalid_token")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/getMe", exc=aiohttp.ClientError("down"))
    resp = await tv.SvitgridTelegramBotView().put(
        _Req(hass, admin.request("telegram_set_bot", token=TOKEN))
    )
    assert (resp.status, _body(resp)["error"]) == (502, "telegram_unreachable")


async def test_delete_bot_signed_clears(hass):
    admin, _, _, state = await _setup(hass, token=True)
    _link(state)
    resp = await tv.SvitgridTelegramBotView().delete(
        _Req(hass, admin.request("telegram_remove_bot"))
    )
    assert resp.status == 200
    assert state.token() is None and state.chats() == []
    unsigned = await tv.SvitgridTelegramBotView().delete(_Req(hass, island_key=None))
    assert unsigned.status in (401, 403)


async def test_test_route(hass, aioclient_mock):
    _, _, _, state = await _setup(hass, token=True)
    resp = await tv.SvitgridTelegramTestView().post(_Req(hass))
    assert (resp.status, _body(resp)["error"]) == (409, "not_linked")
    _link(state)
    aioclient_mock.post(f"{BASE}/sendMessage", json={"ok": True, "result": {}})
    resp = await tv.SvitgridTelegramTestView().post(_Req(hass))
    assert resp.status == 200
    sent = json.dumps(aioclient_mock.mock_calls[-1][2])
    assert "77" in sent


async def test_test_route_blocked_unlinks(hass, aioclient_mock):
    _, _, _, state = await _setup(hass, token=True)
    _link(state)
    aioclient_mock.post(f"{BASE}/sendMessage", status=403, json={"ok": False})
    resp = await tv.SvitgridTelegramTestView().post(_Req(hass))
    assert (resp.status, _body(resp)["error"]) == (409, "not_linked")
    assert state.chat_for("phone-1") is None


async def test_test_route_unreachable_and_invalid(hass, aioclient_mock):
    _, _, _, state = await _setup(hass, token=True)
    _link(state)
    aioclient_mock.post(f"{BASE}/sendMessage", exc=aiohttp.ClientError("down"))
    resp = await tv.SvitgridTelegramTestView().post(_Req(hass))
    assert (resp.status, _body(resp)["error"]) == (502, "telegram_unreachable")
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/sendMessage", status=401, json={"ok": False})
    resp = await tv.SvitgridTelegramTestView().post(_Req(hass))
    assert (resp.status, _body(resp)["error"]) == (400, "invalid_token")
    assert state.status == "invalid_token"


async def test_response_never_contains_token(hass, aioclient_mock):
    admin, _, _, state = await _setup(hass)
    aioclient_mock.get(f"{BASE}/getMe", json={"ok": True, "result": {"username": "my_bot"}})
    aioclient_mock.get(f"{BASE}/getWebhookInfo", json={"ok": True, "result": {"url": ""}})
    aioclient_mock.post(f"{BASE}/sendMessage", json={"ok": True, "result": {}})
    bodies = [
        await tv.SvitgridTelegramBotView().put(
            _Req(hass, admin.request("telegram_set_bot", token=TOKEN))
        ),
        await tv.SvitgridTelegramView().get(_Req(hass)),
        await tv.SvitgridTelegramLinkView().post(_Req(hass, {"language": "en"})),
    ]
    _link(state)
    bodies.append(await tv.SvitgridTelegramTestView().post(_Req(hass)))
    bodies.append(await tv.SvitgridTelegramMeView().delete(_Req(hass)))
    for resp in bodies:
        assert TOKEN not in resp.text and "AAAAAAAA" not in resp.text


async def test_revoking_device_unlinks_its_chat(hass):
    admin, other, ks, state = await _setup(hass, token=True)
    await ks.async_add_island_key("phone-3", "r" * 43, label="Third")
    state_ks = await ks.load()
    state_ks.key_devices[other.key_id] = "phone-2"
    await ks._write(state_ks)
    _link(state, "phone-2", 88)
    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=other.key_id))
    )
    assert resp.status == 200
    assert state.chat_for("phone-2") is None


async def test_island_device_revoke_unlinks_its_chat(hass):
    from custom_components.svitgrid.http_views import SvitgridIslandDeviceRevokeView

    _, _, _, state = await _setup(hass, token=True)
    _link(state, "phone-2", 88)
    view = SvitgridIslandDeviceRevokeView(None)
    resp = await view.post(_Req(hass, {"deviceId": "phone-2"}))
    assert resp.status == 200
    assert state.chat_for("phone-2") is None


async def test_device_for_island_key(hass):
    _, _, ks, _ = await _setup(hass)
    state = await ks.load()
    assert state.device_for_island_key(ISLAND_KEY) == "phone-1"
    assert state.device_for_island_key(OTHER_ISLAND_KEY) == "phone-2"
    assert state.device_for_island_key("nope") is None
    assert state.device_for_island_key("") is None


async def test_entry_removal_unlinks_revoked_phones(hass):
    from custom_components.svitgrid import _revoke_entry_island_keys
    from custom_components.svitgrid.const import LEGACY_ISLAND_DEVICE_ID

    _, _, ks, state = await _setup(hass, token=True)
    cur = await ks.load()
    cur.island_key = "s" * 43
    await ks._write(cur)
    _link(state, "phone-2", 88)
    _link(state, "phone-1", 77)
    _link(state, LEGACY_ISLAND_DEVICE_ID, 99)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"island_key": "s" * 43, "island_device_ids": ["phone-2"]},
    )
    entry.add_to_hass(hass)
    await _revoke_entry_island_keys(hass, entry)
    assert state.chat_for("phone-2") is None
    assert state.chat_for(LEGACY_ISLAND_DEVICE_ID) is None
    assert state.chat_for("phone-1") is not None


async def test_revoking_device_drops_its_pending_link_code(hass):
    admin, other, ks, state = await _setup(hass, token=True)
    state_ks = await ks.load()
    state_ks.key_devices[other.key_id] = "phone-2"
    await ks._write(state_ks)
    code = await state.issue_code("phone-2", "uk", datetime.now(UTC))
    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=other.key_id))
    )
    assert resp.status == 200
    assert await state.claim_code(code, 88, None, datetime.now(UTC)) is None
    assert state.chat_for("phone-2") is None
