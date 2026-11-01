"""The island Telegram routes: auth, errors, and the token never leaving."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

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


# ── the shared contract ─────────────────────────────────────────────────────
# `island_telegram_contract.json` is written from the routes and held by the app
# client and the phone station too. A case here that fails is a disagreement.

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "island_telegram_contract.json").read_text()
)
CT = CONTRACT["token"]
CT_BASE = f"https://api.telegram.org/bot{CT}"
ROUTES = {r["id"]: r for r in CONTRACT["routes"]}
HA_CASES = [c for c in CONTRACT["cases"] if "ha" in c["stations"] and not c.get("clientOnly")]
_VIEWS = {
    "get_status": (tv.SvitgridTelegramView, "get"),
    "put_bot": (tv.SvitgridTelegramBotView, "put"),
    "delete_bot": (tv.SvitgridTelegramBotView, "delete"),
    "post_link": (tv.SvitgridTelegramLinkView, "post"),
    "patch_me": (tv.SvitgridTelegramMeView, "patch"),
    "delete_me": (tv.SvitgridTelegramMeView, "delete"),
    "post_test": (tv.SvitgridTelegramTestView, "post"),
}


def _fill(value):
    if value == "$token":
        return CT
    if isinstance(value, dict):
        return {k: _fill(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v) for v in value]
    return value


def _script_telegram(aioclient_mock, script: str) -> None:
    """Answer the Bot API as the contract's named script says."""
    get_me = {"json": {"ok": True, "result": {"username": CONTRACT["botUsername"]}}}
    webhook = {"json": {"ok": True, "result": {"url": ""}}}
    send = {"json": {"ok": True, "result": {}}}
    if script == "getMe_401":
        get_me = {"status": 401, "json": {"ok": False}}
    elif script == "getMe_down":
        get_me = {"exc": aiohttp.ClientError("down")}
    elif script == "webhook":
        webhook = {"json": {"ok": True, "result": {"url": "https://x.example/hook"}}}
    elif script == "send_403":
        send = {"status": 403, "json": {"ok": False}}
    elif script == "send_401":
        send = {"status": 401, "json": {"ok": False}}
    elif script == "send_down":
        send = {"exc": aiohttp.ClientError("down")}
    aioclient_mock.get(f"{CT_BASE}/getMe", **get_me)
    aioclient_mock.get(f"{CT_BASE}/getWebhookInfo", **webhook)
    aioclient_mock.post(f"{CT_BASE}/sendMessage", **send)


def _ha_request(hass, case, admin, *, replay_of=None):
    wire = case["request"]["ha"]
    island_key = None if case.get("noIslandKey") else ISLAND_KEY
    if wire["auth"] == "keyed":
        return _Req(hass, _fill(wire["body"]), island_key=island_key)
    if replay_of is not None:
        return _Req(hass, replay_of, island_key=island_key)
    fields = _fill(dict(wire["fields"]))
    envelope = admin.request(fields.pop("action"), **fields)
    tamper = case.get("tamper")
    if tamper == "signature":
        envelope["signature"] = "00" * 64
    elif tamper == "stale_ts":
        envelope = admin.signed({**envelope["signedData"], "ts": NOW_MS - 10 * 60 * 1000})
    return _Req(hass, envelope, island_key=island_key)


def _volatile_matches(value, pattern: str) -> bool:
    if not isinstance(value, str):
        return False
    if pattern == "iso8601":
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
        return True
    return re.fullmatch(pattern, value) is not None


def test_contract_routes_are_the_views_routes():
    served = set()
    for view_cls in tv.TELEGRAM_VIEWS:
        for method in ("get", "put", "post", "patch", "delete"):
            if method in vars(view_cls):
                served.add((method.upper(), view_cls.url))
    assert served == {(r["method"], r["path"]) for r in CONTRACT["routes"]}
    for rid, (view_cls, method) in _VIEWS.items():
        assert view_cls.url == ROUTES[rid]["path"]
        assert method.upper() == ROUTES[rid]["method"]


@pytest.mark.parametrize("case", HA_CASES, ids=[c["id"] for c in HA_CASES])
async def test_contract_case(hass, aioclient_mock, case):
    admin, _, _, state = await _setup(hass)
    if case.get("sends"):
        MockConfigEntry(domain=DOMAIN, data={"cloud_ingest_enabled": False}).add_to_hass(hass)
    if case["setup"] in ("bot", "linked"):
        await state.set_bot(CT, CONTRACT["botUsername"])
    if case["setup"] == "linked":
        chat = CONTRACT["linkedChat"]
        state._chats["phone-1"] = Chat(
            "phone-1",
            chat["chatId"],
            chat["username"],
            True,
            chat["language"],
            "2026-10-30T00:00:00+00:00",
        )
    _script_telegram(aioclient_mock, case.get("telegram", "ok"))

    route = ROUTES[case["route"]]
    assert route["auth"]["ha"].split(":")[0] == case["request"]["ha"]["auth"] or case.get(
        "stationOnly"
    )
    view_cls, method = _VIEWS[case["route"]]
    first = _ha_request(hass, case, admin)
    if case.get("tamper") == "replay":
        assert (await getattr(view_cls(), method)(first)).status == 200
        first = _ha_request(hass, case, admin, replay_of=first._body)
    resp = await getattr(view_cls(), method)(first)

    reply = case["reply"]
    assert resp.status == reply["status"], resp.text
    actual = _body(resp)
    expected = dict(reply["body"])
    for key, pattern in reply.get("volatile", {}).items():
        assert _volatile_matches(actual.pop(key, None), pattern), f"{case['id']}: {key}"
        expected.pop(key)
    assert actual == expected
