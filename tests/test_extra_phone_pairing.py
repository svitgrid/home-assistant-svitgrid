"""A second phone joins an existing station over the LAN (owner, 2026-10-14).

Pairing used to refuse with `409 already_configured` once any Svitgrid entry
existed. Now the same LAN pairing adds the phone to that entry, guest or
cloud-paired: the owner opens the pairing in Home Assistant, the app takes the
code from `/hello`, calls `POST /api/svitgrid/pair-local`, and the owner picks
the phone's rights in the approval dialog:

- `pair_approve`: control. The phone gets an island key and its signing key is
  trusted, so its commands and calendar writes pass.
- `pair_approve_view`: view only. The phone gets an island key and no trusted
  signing key, so its writes fail the signature check with 403.

The 200 answer gains `role`. Adding a phone makes no cloud call: the code is
minted on this Home Assistant, and only the LAN can claim it. The five-wrong-
codes lockout and the code's time to live are those of a first pairing.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DOMAIN, MAX_INVERTERS
from custom_components.svitgrid.http_views import SvitgridEventsView, SvitgridHelloView
from custom_components.svitgrid.keystore import (
    ENTRY_ISLAND_DEVICE_IDS,
    ENTRY_TRUSTED_KEY_IDS,
    SvitgridKeystore,
)
from custom_components.svitgrid.signing import generate_keypair, sign_payload
from tests.test_local_pairing import (
    ISLAND_KEY,
    _body,
    _claim,
    _cloud,
    _code,
    _post,
    _Req,
    _shown,
    _start_flow,
)

OWNER_KEY = ("owner-key", "04" + "c" * 128)
SECOND_ISLAND_KEY = "S" * 43


def _phone_body(code: str, **overrides) -> dict:
    """What a second phone sends: its own keys, and no station description."""
    from custom_components.svitgrid.signing import compute_key_id

    priv, pub = generate_keypair()
    key_id = compute_key_id(pub)
    body = {
        "code": code,
        "islandKey": SECOND_ISLAND_KEY,
        "deviceId": "install-second",
        "deviceLabel": "iPhone 15",
        "signingKeyId": key_id,
        "publicKeyHex": pub,
        "signature": sign_payload({"signingKeyId": key_id, "publicKeyHex": pub}, priv),
    }
    body.update(overrides)
    body["_priv"] = priv
    return body


def _wire(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "_priv"}


async def _station(hass, *, local_only: bool) -> MockConfigEntry:
    data = (
        {"local_only": True, "cloud_ingest_enabled": False}
        if local_only
        else {"api_key": "k", "edge_device_id": "ed-1", "cloud_ingest_enabled": False}
    )
    data["inverters"] = [{"inverter_id": "inv-1", "entity_map": {}}]
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=data, title="Дім")
    entry.add_to_hass(hass)
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="" if local_only else "k",
        public_key_hex="04" + "b" * 128,
        private_key_pem="pem",
        signing_key_id="ha-1",
        trusted_key_ids=[OWNER_KEY[0]],
        trusted_public_keys_hex=dict([OWNER_KEY]),
    )
    await ks.async_add_island_key("install-first", ISLAND_KEY, label="Pixel 8")
    hass.data.setdefault(DOMAIN, {})["keystore"] = ks
    return entry


async def _answer(hass, flow_id: str, answer: str) -> dict:
    shown = await _shown(hass, flow_id)
    assert shown["type"] == "menu", shown
    assert shown["step_id"] == "pair_approval_add"
    return await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": answer})


async def _add(hass, flow_id: str, body: dict, answer: str):
    post = await _claim(hass, _wire(body))
    assert not post.done(), _body(post.result())
    result = await _answer(hass, flow_id, answer)
    resp = await post
    if answer != "pair_reject":
        # A rejected pairing keeps waiting for the next claim.
        await hass.async_block_till_done()
    return resp, result


@pytest.fixture(params=[False, True], ids=["cloud-paired", "guest"])
def local_only(request) -> bool:
    return request.param


async def test_adding_a_phone_makes_no_cloud_call(
    hass: HomeAssistant, enable_custom_integrations, local_only
) -> None:
    await _station(hass, local_only=local_only)
    with _cloud(reachable=True) as client:
        result = await _start_flow(hass)
        code = _code(hass)
        hass.config_entries.flow.async_abort(result["flow_id"])

    client.start.assert_not_awaited()
    assert result["type"] == "progress"
    assert result["progress_action"] == "waiting_for_phone"
    assert code is not None and code != "CL0UD7"
    assert result["description_placeholders"]["code"] == code


async def test_hello_publishes_the_code_for_the_second_phone(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        hello = _body(await SvitgridHelloView().get(_Req(hass)))
        code = _code(hass)
        hass.config_entries.flow.async_abort(result["flow_id"])
    assert hello["paired"] is True
    assert hello["pairingPending"] is True
    assert hello["code"] == code


async def test_the_dialog_offers_control_view_only_and_reject(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        post = await _claim(hass, _wire(_phone_body(_code(hass))))
        shown = await _shown(hass, result["flow_id"])
        post.cancel()

    assert shown["type"] == "menu"
    assert shown["step_id"] == "pair_approval_add"
    assert set(shown["menu_options"]) == {"pair_approve", "pair_approve_view", "pair_reject"}
    assert shown["description_placeholders"]["device"] == "iPhone 15"


async def test_approve_with_control_adds_the_phone_to_the_station(
    hass: HomeAssistant, enable_custom_integrations, local_only
) -> None:
    entry = await _station(hass, local_only=local_only)
    inverters_before = list(entry.data["inverters"])
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        resp, ended = await _add(hass, result["flow_id"], body, "pair_approve")

    assert resp.status == 200, _body(resp)
    answer = _body(resp)
    assert answer == {
        "ok": True,
        "stationId": entry.entry_id,
        "version": answer["version"],
        "maxInverters": MAX_INVERTERS,
        "role": "control",
        "stationName": "Дім",
        "inverters": [{"inverterId": "inv-1", "modelId": None, "name": None}],
    }
    assert ended["type"] == "abort"
    assert ended["reason"] == "phone_added"
    # Still one station, with its inverters untouched.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data["inverters"] == inverters_before

    state = await hass.data[DOMAIN]["keystore"].load()
    assert state.island_keys["install-second"]["key"] == SECOND_ISLAND_KEY
    assert state.island_keys["install-second"]["label"] == "iPhone 15"
    # The first phone keeps its key, and the owner's signing key stays trusted.
    assert state.island_keys["install-first"]["key"] == ISLAND_KEY
    assert state.trusted_public_keys_hex[OWNER_KEY[0]] == OWNER_KEY[1]
    assert state.trusted_public_keys_hex[body["signingKeyId"]] == body["publicKeyHex"]
    assert state.key_devices[body["signingKeyId"]] == "install-second"
    # Recorded, so removing the station revokes this phone too.
    assert "install-second" in entry.data[ENTRY_ISLAND_DEVICE_IDS]
    assert body["signingKeyId"] in entry.data[ENTRY_TRUSTED_KEY_IDS]
    # The code is spent.
    assert _code(hass) is None


async def test_approve_view_only_gives_an_island_key_and_no_trust(
    hass: HomeAssistant, enable_custom_integrations, local_only
) -> None:
    entry = await _station(hass, local_only=local_only)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        resp, ended = await _add(hass, result["flow_id"], body, "pair_approve_view")

    assert resp.status == 200, _body(resp)
    assert _body(resp)["role"] == "view"
    assert _body(resp)["stationId"] == entry.entry_id
    assert ended["reason"] == "phone_added"

    state = await hass.data[DOMAIN]["keystore"].load()
    assert state.island_keys["install-second"]["key"] == SECOND_ISLAND_KEY
    assert body["signingKeyId"] not in state.trusted_public_keys_hex
    # Linked anyway, so the owner can revoke the phone by its key.
    assert state.key_devices[body["signingKeyId"]] == "install-second"
    assert "install-second" in entry.data[ENTRY_ISLAND_DEVICE_IDS]
    assert body["signingKeyId"] not in (entry.data.get(ENTRY_TRUSTED_KEY_IDS) or [])


async def test_a_view_only_phone_can_read_and_cannot_write(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        await _add(hass, result["flow_id"], body, "pair_approve_view")

    req = _Req(hass)
    req.headers["X-Island-Key"] = SECOND_ISLAND_KEY
    read = await SvitgridEventsView().get(req)
    assert read.status == 200

    event = {"id": "ev-1", "mode": "sell_to_grid", "enabled": True}
    write = _Req(
        hass,
        {
            "event": event,
            "signingKeyId": body["signingKeyId"],
            "signedEventData": event,
            "signature": sign_payload(event, body["_priv"]),
        },
    )
    write.headers["X-Island-Key"] = SECOND_ISLAND_KEY
    resp = await SvitgridEventsView().post(write)
    assert resp.status == 403
    assert _body(resp)["error"] == "signature_invalid"


async def test_rejecting_the_second_phone_grants_nothing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        resp, after = await _add(hass, result["flow_id"], body, "pair_reject")
        hass.config_entries.flow.async_abort(result["flow_id"])

    assert resp.status == 403
    assert _body(resp) == {"error": "rejected"}
    assert after["type"] == "progress"
    state = await hass.data[DOMAIN]["keystore"].load()
    assert "install-second" not in state.island_keys
    assert body["signingKeyId"] not in state.trusted_public_keys_hex


async def test_a_second_phone_needs_no_inverters(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The station already has its inverters. A body that describes some is
    accepted, and they are ignored."""
    entry = await _station(hass, local_only=True)
    before = list(entry.data["inverters"])
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(
            _code(hass),
            inverters=[{"inverterId": "x", "presetId": "deye-sg04lp3-solarman-v1"}],
            stationName="Інша",
        )
        resp, _ = await _add(hass, result["flow_id"], body, "pair_approve")

    assert resp.status == 200
    assert entry.data["inverters"] == before
    assert entry.title == "Дім"


async def test_five_wrong_codes_still_close_the_pairing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        for left in (4, 3, 2, 1, 0):
            resp = await _post(hass, _wire(_phone_body("WRONG2")))
            assert resp.status == 403
            assert _body(resp) == {"error": "wrong_code", "attemptsLeft": left}
        resp = await _post(hass, _wire(_phone_body(code)))
        ended = await _shown(hass, result["flow_id"])

    assert resp.status == 404
    assert _body(resp)["error"] == "no_pending_pairing"
    assert ended["type"] == "abort"
    assert ended["reason"] == "pairing_cancelled"


async def test_with_a_station_and_nothing_pending_pair_local_is_404(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _station(hass, local_only=False)
    resp = await _post(hass, _wire(_phone_body("ABCDEF")))
    assert resp.status == 404
    assert _body(resp)["error"] == "no_pending_pairing"


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_the_add_phone_dialog_is_translated(lang: str) -> None:
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "custom_components" / "svitgrid"
    data = json.loads((base / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
    config = data["config"]
    step = config["step"]["pair_approval_add"]
    assert "{device}" in step["title"]
    assert set(step["menu_options"]) == {"pair_approve", "pair_approve_view", "pair_reject"}
    assert "{code}" in config["progress"]["waiting_for_phone"]
    assert "{device}" in config["abort"]["phone_added"]


# ── the second phone learns the station's inverters ─────────────────────────


async def test_a_second_phone_learns_each_inverter_and_its_model(
    hass: HomeAssistant, enable_custom_integrations, local_only
) -> None:
    """The second phone brings no station, so it knows no inverter. Its
    editors list actions by `modelId` (contract "Hardening", 2026-10-16)."""
    entry = await _station(hass, local_only=local_only)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "inverters": [
                # Direct harvest: the model is in the harvest config.
                {
                    "inverter_id": "inv-direct",
                    "name": "Deye на даху",
                    "harvest_config": {"protocol": "solarman_v5", "model_id": "deye_sg04lp3"},
                },
                # Relay through a preset that names its model.
                {
                    "inverter_id": "inv-preset",
                    "brand": "Anenji",
                    "model": "ANJ-6200",
                    "preset_id": "anenji-anj-6200-smartess-v1",
                },
                # Relay through a preset that names none.
                {"inverter_id": "inv-relay", "preset_id": "deye-sg04lp3-solarman-v1"},
            ],
        },
    )
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        resp, _ = await _add(hass, result["flow_id"], body, "pair_approve_view")

    assert resp.status == 200, _body(resp)
    answer = _body(resp)
    assert answer["role"] == "view"
    assert answer["stationName"] == "Дім"
    assert answer["inverters"] == [
        {"inverterId": "inv-direct", "modelId": "deye_sg04lp3", "name": "Deye на даху"},
        {"inverterId": "inv-preset", "modelId": "anenji_anj_6200", "name": "Anenji ANJ-6200"},
        {"inverterId": "inv-relay", "modelId": None, "name": None},
    ]
