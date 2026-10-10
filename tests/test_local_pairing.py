"""Guest pairing over the LAN: one code, two ways to claim it.

The config flow shows a pairing code. A signed-in app claims it through the
cloud (`/ha-pairing/claim`, polled here through `/status`); a guest app with
no account claims it on the LAN with `POST /api/svitgrid/pair-local`. The
first claim wins and the other answers `already_claimed` or stops polling.

When the cloud cannot be reached, the flow mints the code itself, from the
cloud's alphabet, and only the LAN can claim it.

The request and response shapes here are the contract the Svitgrid app is
built against (spec "LAN pairing contract", 2026-10-10). Do not change a
field name, status code or error code to make a test pass.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import (
    DOMAIN,
    LOCAL_PAIRING_GRANT,
    MAX_INVERTERS,
    PAIRING_CODE_ALPHABET,
)
from custom_components.svitgrid.http_views import (
    SvitgridHelloView,
    SvitgridLocalPresetsView,
    SvitgridPairLocalView,
)
from custom_components.svitgrid.local_pairing import get_session
from custom_components.svitgrid.pairing_client import PairingClaimed, PairingPending
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload

ISLAND_KEY = "A" * 22 + "b" * 21  # 43 base64url characters


class _Req:
    """An unauthenticated LAN request: no HA session, no island key."""

    def __init__(self, hass, body=None, *, raw: bytes | None = None):
        self.app = {"hass": hass}
        self.headers: dict = {}
        self.query: dict = {}
        self._body = body
        self._raw = raw

    def get(self, key, default=None):
        return default

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.body)


def _app_key():
    priv, pub = generate_keypair()
    key_id = compute_key_id(pub)
    sig = sign_payload({"signingKeyId": key_id, "publicKeyHex": pub}, priv)
    return key_id, pub, sig


def _pair_body(code: str, **overrides) -> dict:
    key_id, pub, sig = _app_key()
    body = {
        "code": code,
        "islandKey": ISLAND_KEY,
        "deviceId": "install-abc",
        "deviceLabel": "Pixel 8",
        "signingKeyId": key_id,
        "publicKeyHex": pub,
        "signature": sig,
        "stationName": "Дім",
        "presetId": "deye-sg04lp3-solarman-v1",
        "inverters": [
            {
                "inverterId": "local-inv-3f9a",
                "name": "Deye",
                "presetId": "deye-sg04lp3-solarman-v1",
                "harvestConfig": {
                    "protocol": "solarman_v5",
                    "ip": "192.168.1.50",
                    "port": 8899,
                    "slaveId": 1,
                    "modelId": "deye_sg04lp3",
                    "loggerSerial": "2712345678",
                },
            },
            {
                "inverterId": "local-inv-relay",
                "name": "Second",
                "presetId": "deye-sg04lp3-solarman-v1",
            },
        ],
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _no_browser_grace():
    """No browser in these tests, so pair-local finishes the flow at once."""
    with patch("custom_components.svitgrid.local_pairing.BROWSER_GRACE_S", 0):
        yield


def _code(hass) -> str | None:
    pending = (hass.data.get(DOMAIN) or {}).get("pending_pairing") or {}
    return pending.get("code")


@contextmanager
def _cloud(*, reachable: bool, status=None):
    """Patch the cloud pairing client and keep setup out of the way."""
    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as cls,
        patch("custom_components.svitgrid.async_setup_entry", AsyncMock(return_value=True)),
    ):
        client = cls.return_value
        if reachable:
            client.start = AsyncMock(
                return_value={"secret": "s" * 40, "code": "CL0UD7", "expiresIn": 300}
            )
        else:
            client.start = AsyncMock(side_effect=OSError("network unreachable"))
        client.get_status = status or AsyncMock(return_value=PairingPending())
        client.finalize = AsyncMock()
        yield client


async def _start_flow(hass) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _post(hass, body) -> object:
    return await SvitgridPairLocalView().post(_Req(hass, body))


# ── a code without the cloud ────────────────────────────────────────────────


async def test_without_the_cloud_the_flow_mints_a_local_code(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)

        assert result["type"] == "progress"
        code = _code(hass)
        assert code is not None
        assert len(code) == 6
        assert set(code) <= set(PAIRING_CODE_ALPHABET)
        assert result["description_placeholders"]["code"] == code


async def test_with_the_cloud_the_cloud_code_is_shown(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=True):
        await _start_flow(hass)
        assert _code(hass) == "CL0UD7"


# ── pair-local completes the flow ───────────────────────────────────────────


async def test_pair_local_creates_a_local_only_entry(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        body = _pair_body(_code(hass))
        resp = await _post(hass, body)
        await hass.async_block_till_done()

    assert resp.status == 200, _body(resp)
    answer = _body(resp)
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert answer == {
        "ok": True,
        "stationId": entry.entry_id,
        "version": answer["version"],
        "maxInverters": MAX_INVERTERS,
    }
    assert answer["version"][0].isdigit()

    data = entry.data
    assert entry.title == "Дім"
    assert data["local_only"] is True
    assert data["cloud_ingest_enabled"] is False
    for cloud_field in ("api_key", "api_base", "edge_device_id", "household_id"):
        assert cloud_field not in data
    # The integration's own signing keypair, minted locally.
    assert data["signing_key_id"]
    assert data["private_key_pem"].startswith("-----BEGIN PRIVATE KEY-----")
    assert data["public_key_hex"].startswith("04")


async def test_the_entry_carries_the_grant_and_the_inverters_by_app_id(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        body = _pair_body(_code(hass))
        await _post(hass, body)
        await hass.async_block_till_done()

    data = hass.config_entries.async_entries(DOMAIN)[0].data
    grant = data[LOCAL_PAIRING_GRANT]
    assert grant["deviceId"] == "install-abc"
    assert grant["islandKey"] == ISLAND_KEY
    assert grant["deviceLabel"] == "Pixel 8"
    assert grant["signingKeyId"] == body["signingKeyId"]
    assert grant["publicKeyHex"] == body["publicKeyHex"]
    assert grant["pairedAt"].endswith("Z")

    direct, relay = data["inverters"]
    assert direct["inverter_id"] == "local-inv-3f9a"
    assert direct["harvest_config"]["model_id"] == "deye_sg04lp3"
    assert direct["harvest_config"]["logger_serial"] == "2712345678"
    assert direct["harvest_config"]["slave_id"] == 1
    assert relay["inverter_id"] == "local-inv-relay"
    assert "harvest_config" not in relay
    # Relay inverters read the bundled preset's entities.
    assert relay["entity_map"]["batterySoc"] == "sensor.inverter_battery"
    assert relay["preset_id"] == "deye-sg04lp3-solarman-v1"
    assert relay["brand"] == "Deye"
    assert relay["name"] == "Second"


async def test_the_code_stops_being_published_after_a_lan_claim(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        await _post(hass, _pair_body(_code(hass)))
        await hass.async_block_till_done()

    assert _code(hass) is None
    hello = _body(await SvitgridHelloView().get(_Req(hass)))
    assert hello["pairingPending"] is False
    assert "code" not in hello


async def test_a_lowercase_code_is_accepted(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await _post(hass, _pair_body(_code(hass).lower()))
        await hass.async_block_till_done()
    assert resp.status == 200


# ── first claim wins ────────────────────────────────────────────────────────


async def test_with_the_cloud_up_a_lan_claim_wins_and_polling_stops(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=True) as client:
        await _start_flow(hass)
        resp = await _post(hass, _pair_body("CL0UD7"))
        await hass.async_block_till_done()
        polls_after_claim = client.get_status.await_count
        await asyncio.sleep(0)
        await hass.async_block_till_done()

        assert resp.status == 200, _body(resp)
        assert client.get_status.await_count == polls_after_claim
        client.finalize.assert_not_awaited()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["local_only"] is True


async def test_a_second_lan_claim_is_already_claimed(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        code = _code(hass)
        assert (await _post(hass, _pair_body(code))).status == 200
        await hass.async_block_till_done()
        resp = await _post(hass, _pair_body(code))

    assert resp.status == 409
    assert _body(resp)["error"] == "already_claimed"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_after_a_cloud_claim_pair_local_is_already_claimed(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    claimed = PairingClaimed(household_id="h1", preset_id=None)
    # The cloud claim is seen on the first poll; finalize then hangs, so the
    # flow sits between the claim and the entry.
    finalize_started = asyncio.Event()
    never = asyncio.Event()

    async def _slow_finalize(**_):
        finalize_started.set()
        await never.wait()

    async def _instant_sleep(_):
        return None

    with (
        _cloud(reachable=True, status=AsyncMock(return_value=claimed)) as client,
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_instant_sleep),
    ):
        client.finalize = AsyncMock(side_effect=_slow_finalize)
        init = asyncio.ensure_future(_start_flow(hass))
        await finalize_started.wait()
        resp = await _post(hass, _pair_body("CL0UD7"))
        init.cancel()

    assert resp.status == 409
    assert _body(resp)["error"] == "already_claimed"


# ── wrong codes ─────────────────────────────────────────────────────────────


async def test_a_wrong_code_reports_attempts_left(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await _post(hass, _pair_body("WRONG2"))

    assert resp.status == 403
    assert _body(resp) == {"error": "wrong_code", "attemptsLeft": 4}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_the_fifth_wrong_code_cancels_the_pairing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        answers = [_body(await _post(hass, _pair_body("WRONG2"))) for _ in range(5)]
        await hass.async_block_till_done()

        assert [a["attemptsLeft"] for a in answers] == [4, 3, 2, 1, 0]
        # The right code is too late now.
        resp = await _post(hass, _pair_body(code))
        assert resp.status == 404
        assert _body(resp)["error"] == "no_pending_pairing"
        assert _code(hass) is None

        # The flow ends, telling the owner to start again.
        ended = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert ended["type"] == "abort"
        assert ended["reason"] == "pairing_cancelled"


async def test_a_wrong_code_does_not_leak_the_right_one(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await _post(hass, _pair_body("WRONG2"))
    assert _code(hass) not in resp.body.decode()


# ── no pending pairing ──────────────────────────────────────────────────────


async def test_pair_local_without_a_pending_pairing_is_404(hass: HomeAssistant) -> None:
    resp = await _post(hass, _pair_body("ABCDEF"))
    assert resp.status == 404
    assert _body(resp)["error"] == "no_pending_pairing"


async def test_local_presets_without_a_pending_pairing_is_404(hass: HomeAssistant) -> None:
    resp = await SvitgridLocalPresetsView().get(_Req(hass))
    assert resp.status == 404
    assert _body(resp)["error"] == "no_pending_pairing"


async def test_closing_the_flow_ends_the_pending_pairing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()
        resp = await _post(hass, _pair_body(code))

    assert resp.status == 404
    assert _code(hass) is None


# ── local presets ───────────────────────────────────────────────────────────


async def test_local_presets_lists_the_bundled_presets_while_pending(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await SvitgridLocalPresetsView().get(_Req(hass))

    assert resp.status == 200
    presets = _body(resp)["presets"]
    assert presets
    ids = {p["id"] for p in presets}
    assert "deye-sg04lp3-solarman-v1" in ids
    assert set(presets[0]) == {
        "id",
        "version",
        "brand",
        "model",
        "phases",
        "hasBattery",
        "commandsCount",
    }


# ── malformed requests ──────────────────────────────────────────────────────


def _bad_bodies():
    key_id, pub, sig = _app_key()
    _, other_pub, _ = _app_key()
    other_priv, _ = generate_keypair()
    wrong_sig = sign_payload({"signingKeyId": key_id, "publicKeyHex": pub}, other_priv)
    return [
        pytest.param({"islandKey": "short"}, 400, "bad_request", id="island key too short"),
        pytest.param({"islandKey": "!" * 43}, 400, "bad_request", id="island key not base64url"),
        pytest.param({"deviceId": ""}, 400, "bad_request", id="no device id"),
        pytest.param({"inverters": []}, 400, "bad_request", id="no inverters"),
        pytest.param(
            {"inverters": [{"inverterId": "x", "name": "n"}]},
            400,
            "bad_request",
            id="inverter with neither presetId nor harvestConfig",
        ),
        pytest.param(
            {"inverters": [{"inverterId": "x", "presetId": "no-such-preset-v1"}]},
            400,
            "bad_request",
            id="unknown preset and no harvestConfig",
        ),
        pytest.param(
            {"publicKeyHex": "04abc", "signingKeyId": "x"},
            400,
            "bad_public_key",
            id="malformed public key",
        ),
        pytest.param(
            {"publicKeyHex": other_pub}, 400, "key_id_mismatch", id="key id of another key"
        ),
        pytest.param(
            {"signingKeyId": key_id, "publicKeyHex": pub, "signature": wrong_sig},
            403,
            "signature_invalid",
            id="signed by another key",
        ),
        pytest.param(
            {
                "inverters": [
                    {"inverterId": f"inv-{i}", "presetId": "deye-sg04lp3-solarman-v1"}
                    for i in range(MAX_INVERTERS + 1)
                ]
            },
            422,
            "too_many_inverters",
            id="more inverters than supported",
        ),
    ]


@pytest.mark.parametrize(("overrides", "status", "error"), _bad_bodies())
async def test_a_malformed_claim_is_refused_and_claims_nothing(
    hass: HomeAssistant, enable_custom_integrations, overrides, status, error
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        code = _code(hass)
        resp = await _post(hass, _pair_body(code, **overrides))

        assert resp.status == status, _body(resp)
        assert _body(resp)["error"] == error
        if error == "too_many_inverters":
            assert _body(resp)["maxInverters"] == MAX_INVERTERS
        # Still pending: a malformed claim spends no attempt and claims nothing.
        assert _code(hass) == code
        assert (await _post(hass, _pair_body(code))).status == 200
        await hass.async_block_till_done()


async def test_a_body_that_is_not_json_is_bad_request(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await SvitgridPairLocalView().post(_Req(hass, raw=b"not json"))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


# ── /hello ──────────────────────────────────────────────────────────────────


async def test_hello_says_this_is_home_assistant_with_local_pairing(hass: HomeAssistant) -> None:
    body = _body(await SvitgridHelloView().get(_Req(hass)))
    assert body["kind"] == "home_assistant"
    assert body["localPairing"] is True


# ── flow endings ────────────────────────────────────────────────────────────


async def test_a_window_that_runs_out_in_the_background_ends_as_expired(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """A progress step may only move to progress-done. Aborting from it
    directly raised ValueError when the window ran out while the owner was
    looking at the code, and the dialog kept spinning."""
    with (
        _cloud(reachable=False),
        patch("custom_components.svitgrid.config_flow.PAIRING_MAX_POLL_DURATION_S", 0.01),
    ):
        result = await _start_flow(hass)
        assert result["type"] == "progress"
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        ended = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert ended["type"] == "abort"
    assert ended["reason"] == "pairing_expired"
    assert _code(hass) is None


async def test_an_open_browser_finishes_the_flow_and_pair_local_reports_its_entry(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """With the code on screen, the browser configures the flow as soon as the
    claim lands. pair-local waits for that rather than racing it, and answers
    with the entry the browser created."""
    with (
        _cloud(reachable=False),
        patch("custom_components.svitgrid.local_pairing.BROWSER_GRACE_S", 5),
    ):
        result = await _start_flow(hass)
        session = get_session(hass)
        post = asyncio.ensure_future(_post(hass, _pair_body(_code(hass))))
        for _ in range(500):
            if session.claimed_by is not None:
                break
            await asyncio.sleep(0.01)
        assert session.claimed_by == "lan"
        await hass.async_block_till_done()
        # What the frontend does on the progress event.
        finished = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert finished["type"] == "create_entry"
        resp = await post
        await hass.async_block_till_done()

    assert resp.status == 200
    assert _body(resp)["stationId"] == finished["result"].entry_id
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
