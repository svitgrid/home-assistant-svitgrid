"""Guest pairing over the LAN: one code, two ways to claim it, and the owner decides.

The config flow shows a pairing code. A signed-in app claims it through the
cloud (`/ha-pairing/claim`, polled here through `/status`); a guest app with
no account claims it on the LAN with `POST /api/svitgrid/pair-local`.

`/hello` publishes the code to anyone on the network, so the code alone cannot
authorise a LAN claim. Once the code, body and signature check out, the
pairing dialog asks the owner «Підключити <deviceLabel>?», and pair-local
holds the request until the owner answers, for up to 120 seconds. Only an
approved claim creates the entry, adds the island key and trusts the signing
key.

When the cloud cannot be reached, the flow mints the code itself, from the
cloud's alphabet, and only the LAN can claim it. One install has one owner:
pair-local refuses while any Svitgrid entry exists.

The request and response shapes here are the contract the Svitgrid app is
built against (spec "LAN pairing contract" and "Approval in Home Assistant",
2026-10-10). Do not change a field name, status code or error code to make a
test pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.local_pairing import get_session
from custom_components.svitgrid.pairing_client import PairingClaimed, PairingPending
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload

ISLAND_KEY = "A" * 22 + "b" * 21  # 43 base64url characters

_FINALIZE = {
    "edgeDeviceId": "ed-1",
    "hardwareId": "ha-1",
    "apiKey": "cloud-key",
    "householdId": "h1",
    "presetId": None,
    "trustedKeys": [],
    "entityMap": {"batterySoc": "sensor.soc"},
    "brand": "Deye",
    "model": "SG04LP3",
    "phases": 3,
    "hasBattery": True,
    "pvStrings": 2,
    "commands": [],
}


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


def _code(hass) -> str | None:
    pending = (hass.data.get(DOMAIN) or {}).get("pending_pairing") or {}
    return pending.get("code")


@pytest.fixture(autouse=True)
def _fast_cloud_poll():
    with patch("custom_components.svitgrid.config_flow.PAIRING_POLL_INTERVAL_S", 0.01):
        yield


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
        client.finalize = AsyncMock(return_value=dict(_FINALIZE))
        yield client


async def _start_flow(hass) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _post(hass, body) -> object:
    return await SvitgridPairLocalView().post(_Req(hass, body))


async def _until(predicate, what: str) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _claim(hass, body) -> asyncio.Future:
    """Send pair-local and return once it is waiting for the owner (or done)."""
    session = get_session(hass)
    post = asyncio.ensure_future(_post(hass, body))
    await _until(lambda: post.done() or session.candidate is not None, "the approval request")
    return post


async def _shown(hass, flow_id: str) -> dict:
    """What the dialog shows once it reacts to the flow's progress event."""
    for _ in range(500):
        shown = await hass.config_entries.flow.async_configure(flow_id)
        if shown["type"] != "progress":
            return shown
        await asyncio.sleep(0.01)
    return shown


async def _answer(hass, flow_id: str, answer: str) -> dict:
    """What the owner does in the dialog: see the question, tap an answer."""
    shown = await _shown(hass, flow_id)
    assert shown["type"] == "menu", shown
    assert shown["step_id"] == "pair_approval"
    return await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": answer})


async def _pair(hass, flow_id: str, body) -> object:
    """A LAN claim the owner approves."""
    post = await _claim(hass, body)
    assert not post.done(), _body(post.result())
    await _answer(hass, flow_id, "pair_approve")
    resp = await post
    await hass.async_block_till_done()
    return resp


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


# ── the owner approves ──────────────────────────────────────────────────────


async def test_the_dialog_asks_the_owner_naming_the_phone(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        post = await _claim(hass, _pair_body(_code(hass)))
        shown = await _shown(hass, result["flow_id"])
        post.cancel()

    assert shown["type"] == "menu"
    assert shown["step_id"] == "pair_approval"
    assert set(shown["menu_options"]) == {"pair_approve", "pair_reject"}
    assert shown["description_placeholders"]["device"] == "Pixel 8"


async def test_nothing_is_granted_while_the_owner_has_not_answered(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        post = await _claim(hass, _pair_body(_code(hass)))

        assert not post.done()
        assert not hass.config_entries.async_entries(DOMAIN)
        assert await SvitgridKeystore(hass).load() is None
        post.cancel()


async def test_an_approved_claim_creates_a_local_only_entry(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        resp = await _pair(hass, result["flow_id"], _pair_body(_code(hass)))

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
        result = await _start_flow(hass)
        body = _pair_body(_code(hass))
        await _pair(hass, result["flow_id"], body)

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


async def test_the_code_stops_being_published_after_an_approved_claim(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        await _pair(hass, result["flow_id"], _pair_body(_code(hass)))

    assert _code(hass) is None
    hello = _body(await SvitgridHelloView().get(_Req(hass)))
    assert hello["pairingPending"] is False
    assert "code" not in hello


async def test_a_lowercase_code_is_accepted(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        resp = await _pair(hass, result["flow_id"], _pair_body(_code(hass).lower()))
    assert resp.status == 200


async def test_a_second_claim_after_approval_is_already_claimed(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        assert (await _pair(hass, result["flow_id"], _pair_body(code))).status == 200
        # The entry exists now, and one install has one owner.
        resp = await _post(hass, _pair_body(code))

    assert resp.status == 409
    assert _body(resp)["error"] in {"already_claimed", "already_configured"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ── the owner rejects, or does not answer ───────────────────────────────────


async def test_a_rejected_claim_is_403_and_the_pairing_stays_pending(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        post = await _claim(hass, _pair_body(code))
        after = await _answer(hass, result["flow_id"], "pair_reject")
        resp = await post

        assert resp.status == 403
        assert _body(resp) == {"error": "rejected"}
        assert not hass.config_entries.async_entries(DOMAIN)
        assert await SvitgridKeystore(hass).load() is None
        # Back to waiting, with the same code on offer.
        assert after["type"] == "progress"
        assert _code(hass) == code
        # The right phone can still pair.
        assert (await _pair(hass, result["flow_id"], _pair_body(code))).status == 200


async def test_no_answer_in_time_is_408_and_the_pairing_stays_pending(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with (
        _cloud(reachable=False),
        patch("custom_components.svitgrid.local_pairing.APPROVAL_TIMEOUT_S", 0.5),
    ):
        result = await _start_flow(hass)
        code = _code(hass)
        post = await _claim(hass, _pair_body(code))
        # The question is on screen, and the owner walks away.
        assert (await _shown(hass, result["flow_id"]))["type"] == "menu"
        resp = await post

        assert resp.status == 408
        assert _body(resp) == {"error": "approval_timeout"}
        assert not hass.config_entries.async_entries(DOMAIN)
        assert _code(hass) == code

        # The owner taps Approve on the stale question: nothing is created,
        # and the dialog goes back to waiting.
        stale = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "pair_approve"}
        )
        assert stale["type"] == "progress"
        assert not hass.config_entries.async_entries(DOMAIN)

    # The app retries, and this time the owner answers.
    with _cloud(reachable=False):
        resp = await _pair(hass, result["flow_id"], _pair_body(code))
    assert resp.status == 200


async def test_the_default_wait_for_the_owner_is_120_seconds() -> None:
    from custom_components.svitgrid import local_pairing

    assert local_pairing.APPROVAL_TIMEOUT_S == 120


async def test_a_second_claim_while_one_awaits_approval_is_already_claimed(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Refused rather than queued: a queue would let a stranger's request sit
    behind the owner's and be approved by a tap meant for the phone in hand."""
    with _cloud(reachable=False):
        await _start_flow(hass)
        code = _code(hass)
        first = await _claim(hass, _pair_body(code))
        resp = await _post(hass, _pair_body(code, deviceLabel="Other"))
        first.cancel()

    assert resp.status == 409
    assert _body(resp)["error"] == "already_claimed"


async def test_a_dropped_request_withdraws_the_question(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        post = await _claim(hass, _pair_body(_code(hass)))
        post.cancel()
        session = get_session(hass)
        await _until(lambda: session.candidate is None, "the question to be withdrawn")


async def test_a_stale_question_cannot_approve_a_different_phone(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The dialog asked about phone A. A's request timed out and phone B
    claimed. An Approve tap on A's question must not approve B: the owner
    has to see B's name first."""
    with (
        _cloud(reachable=False),
        patch("custom_components.svitgrid.local_pairing.APPROVAL_TIMEOUT_S", 0.3),
    ):
        result = await _start_flow(hass)
        code = _code(hass)
        first = await _claim(hass, _pair_body(code, deviceLabel="Phone A"))
        shown = await _shown(hass, result["flow_id"])
        assert shown["description_placeholders"]["device"] == "Phone A"
        assert (await first).status == 408

    with _cloud(reachable=False):
        second = await _claim(hass, _pair_body(code, deviceLabel="Phone B"))
        tapped = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "pair_approve"}
        )
        assert not hass.config_entries.async_entries(DOMAIN)
        assert not second.done()
        # The dialog moves on to ask about B by name.
        if tapped["type"] == "progress":
            tapped = await _shown(hass, result["flow_id"])
        assert tapped["type"] == "menu"
        assert tapped["description_placeholders"]["device"] == "Phone B"
        second.cancel()


async def test_pair_local_after_the_window_ends_is_404(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        code = _code(hass)
        session = get_session(hass)
        session.expires_at = hass.loop.time() - 1
        resp = await _post(hass, _pair_body(code))
    assert resp.status == 404
    assert _body(resp)["error"] == "no_pending_pairing"


# ── first claim wins ────────────────────────────────────────────────────────


async def test_with_the_cloud_up_an_approved_lan_claim_wins_and_polling_stops(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=True) as client:
        result = await _start_flow(hass)
        resp = await _pair(hass, result["flow_id"], _pair_body("CL0UD7"))
        polls = client.get_status.await_count
        await asyncio.sleep(0.05)

        assert resp.status == 200, _body(resp)
        assert client.get_status.await_count == polls
        client.finalize.assert_not_awaited()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["local_only"] is True


async def test_after_a_cloud_claim_pair_local_is_already_claimed(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    claimed = PairingClaimed(household_id="h1", preset_id=None)
    with _cloud(reachable=True, status=AsyncMock(return_value=claimed)):
        await _start_flow(hass)
        session = get_session(hass)
        await _until(lambda: session.claimed_by == "cloud", "the cloud claim")
        resp = await _post(hass, _pair_body("CL0UD7"))

    assert resp.status == 409
    assert _body(resp)["error"] == "already_claimed"


def _status_after(flag: dict):
    claimed = PairingClaimed(household_id="h1", preset_id=None)

    async def _status(_secret):
        return claimed if flag.get("claimed") else PairingPending()

    return AsyncMock(side_effect=_status)


async def test_a_cloud_claim_during_approval_loses_to_an_approval_and_is_logged(
    hass: HomeAssistant, enable_custom_integrations, caplog
) -> None:
    flag: dict = {}
    with _cloud(reachable=True, status=_status_after(flag)) as client:
        result = await _start_flow(hass)
        post = await _claim(hass, _pair_body("CL0UD7"))
        flag["claimed"] = True
        session = get_session(hass)
        await _until(lambda: session.cloud_claim is not None, "the cloud claim")
        with caplog.at_level(logging.WARNING):
            await _answer(hass, result["flow_id"], "pair_approve")
        resp = await post
        await hass.async_block_till_done()

        assert resp.status == 200
        client.finalize.assert_not_awaited()
    assert any(
        r.levelno == logging.WARNING and "cloud" in r.getMessage().lower() for r in caplog.records
    )


async def test_a_cloud_claim_during_approval_wins_when_the_owner_rejects(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    flag: dict = {}
    with _cloud(reachable=True, status=_status_after(flag)) as client:
        result = await _start_flow(hass)
        post = await _claim(hass, _pair_body("CL0UD7"))
        flag["claimed"] = True
        session = get_session(hass)
        await _until(lambda: session.cloud_claim is not None, "the cloud claim")
        after = await _answer(hass, result["flow_id"], "pair_reject")
        resp = await post
        await hass.async_block_till_done()

        assert resp.status == 403
        client.finalize.assert_awaited_once()
    assert after["type"] == "create_entry"
    assert after["data"]["api_key"] == "cloud-key"


# ── the cloud failing ───────────────────────────────────────────────────────


async def test_network_errors_while_polling_do_not_end_the_cloud_path(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    claimed = PairingClaimed(household_id="h1", preset_id=None)
    status = AsyncMock(side_effect=[aiohttp.ClientConnectionError("dns"), TimeoutError(), claimed])
    with _cloud(reachable=True, status=status):
        result = await _start_flow(hass)
        await _until(lambda: get_session(hass).claimed_by == "cloud", "the cloud claim")
        done = await _shown(hass, result["flow_id"])

    assert done["type"] == "create_entry"
    assert done["data"]["api_key"] == "cloud-key"


async def test_a_cloud_that_fails_after_start_leaves_the_lan_path_open(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=True, status=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await _start_flow(hass)
        await asyncio.sleep(0.05)
        assert _code(hass) == "CL0UD7"
        resp = await _pair(hass, result["flow_id"], _pair_body("CL0UD7"))
    assert resp.status == 200


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


async def test_without_the_cloud_five_wrong_codes_end_the_pairing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        answers = [_body(await _post(hass, _pair_body("WRONG2"))) for _ in range(5)]
        await hass.async_block_till_done()

        assert [a["attemptsLeft"] for a in answers] == [4, 3, 2, 1, 0]
        resp = await _post(hass, _pair_body(code))
        assert resp.status == 404
        assert _body(resp)["error"] == "no_pending_pairing"
        assert _code(hass) is None

        ended = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert ended["type"] == "abort"
        assert ended["reason"] == "pairing_cancelled"


async def test_five_wrong_codes_end_only_the_lan_half(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The signed-in owner's cloud claim of the same code still works."""
    flag: dict = {}
    with _cloud(reachable=True, status=_status_after(flag)):
        result = await _start_flow(hass)
        for _ in range(5):
            await _post(hass, _pair_body("WRONG2"))
        resp = await _post(hass, _pair_body("CL0UD7"))
        assert resp.status == 404
        assert _body(resp)["error"] == "no_pending_pairing"
        # Still on offer for the signed-in app.
        assert _code(hass) == "CL0UD7"

        flag["claimed"] = True
        await _until(lambda: get_session(hass).claimed_by == "cloud", "the cloud claim")
        done = await _shown(hass, result["flow_id"])

    assert done["type"] == "create_entry"
    assert done["data"]["api_key"] == "cloud-key"


async def test_a_wrong_code_does_not_leak_the_right_one(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await _post(hass, _pair_body("WRONG2"))
    assert _code(hass) not in resp.body.decode()


# ── one install, one owner ──────────────────────────────────────────────────


def _existing_entry(hass) -> None:
    MockConfigEntry(domain=DOMAIN, version=2, data={"api_key": "k"}).add_to_hass(hass)


async def test_pair_local_refuses_when_a_station_exists(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    _existing_entry(hass)
    with _cloud(reachable=True):
        await _start_flow(hass)
        resp = await _post(hass, _pair_body("CL0UD7"))
    assert resp.status == 409
    assert _body(resp)["error"] == "already_configured"


async def test_pair_local_refuses_when_a_station_exists_and_nothing_is_pending(
    hass: HomeAssistant,
) -> None:
    _existing_entry(hass)
    resp = await _post(hass, _pair_body("ABCDEF"))
    assert resp.status == 409
    assert _body(resp)["error"] == "already_configured"


async def test_no_local_code_is_minted_when_a_station_exists(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Without the cloud, a local code could only ever be claimed on the LAN,
    which a configured install refuses. So the flow says the cloud is down."""
    _existing_entry(hass)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"
    assert _code(hass) is None


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


async def test_closing_the_flow_answers_a_waiting_claim(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        post = await _claim(hass, _pair_body(_code(hass)))
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()
        resp = await post

    assert resp.status == 404
    assert _body(resp)["error"] == "no_pending_pairing"


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
async def test_a_malformed_claim_is_refused_and_asks_nobody(
    hass: HomeAssistant, enable_custom_integrations, overrides, status, error
) -> None:
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        code = _code(hass)
        resp = await _post(hass, _pair_body(code, **overrides))

        assert resp.status == status, _body(resp)
        assert _body(resp)["error"] == error
        if error == "too_many_inverters":
            assert _body(resp)["maxInverters"] == MAX_INVERTERS
        # No question for the owner, no attempt spent, nothing claimed.
        assert get_session(hass).candidate is None
        assert _code(hass) == code
        assert (await _pair(hass, result["flow_id"], _pair_body(code))).status == 200


async def test_a_body_that_is_not_json_is_bad_request(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with _cloud(reachable=False):
        await _start_flow(hass)
        resp = await SvitgridPairLocalView().post(_Req(hass, raw=b"not json"))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


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


# ── /hello and translations ─────────────────────────────────────────────────


async def test_hello_says_this_is_home_assistant_with_local_pairing(hass: HomeAssistant) -> None:
    body = _body(await SvitgridHelloView().get(_Req(hass)))
    assert body["kind"] == "home_assistant"
    assert body["localPairing"] is True


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_the_approval_question_is_translated(lang: str) -> None:
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "custom_components" / "svitgrid"
    data = json.loads((base / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
    step = data["config"]["step"]["pair_approval"]
    assert "{device}" in step["title"]
    assert set(step["menu_options"]) == {"pair_approve", "pair_reject"}
    if lang == "uk":
        assert step["title"].startswith("Підключити")
