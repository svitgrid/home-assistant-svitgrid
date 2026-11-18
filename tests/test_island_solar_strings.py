"""The app hands Home Assistant each inverter's string setup (svitgrid D-2155).

The island runs the cloud's string-outage rule, which needs every string's
`peakPowerWp`. The app sends `solarConfig.strings` to
`POST /api/svitgrid/island/solar-strings`, signed as the other true-island
writes are, when the owner saves the strings and when a station moves to Home
Assistant. Home Assistant keeps the id and `peakPowerWp` of each string, and the
status advertises `string_outage` so the app knows it may send them.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.island_lan import (
    CAPABILITIES,
    SvitgridIslandSolarStringsView,
)
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload
from custom_components.svitgrid.solar_strings import SolarStringsStore, solar_strings_store

ISLAND_KEY = "p" * 43
NOW_MS = 1_760_000_000_000
TWO = [{"id": "PV1", "peakPowerWp": 3000}, {"id": "PV2", "peakPowerWp": 2700}]


class _Req:
    def __init__(self, hass, body=None, *, island_key: str | None = ISLAND_KEY):
        self.app = {"hass": hass}
        self.headers = {"X-Island-Key": island_key} if island_key else {}
        self.query: dict = {}
        self._body = body

    async def json(self):
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Admin:
    def __init__(self) -> None:
        self.priv, self.pub = generate_keypair()
        self.key_id = compute_key_id(self.pub)

    def request(self, action="solar_strings", **fields) -> dict:
        signed = {"action": action, "nonce": uuid.uuid4().hex, "ts": NOW_MS, **fields}
        return {
            "signingKeyId": self.key_id,
            "signedData": signed,
            "signature": sign_payload(signed, self.priv),
        }


@pytest.fixture(autouse=True)
def _pinned_clock():
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


async def _station(hass, *admins: _Admin) -> MockConfigEntry:
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="cloud-api-key",
        public_key_hex="04" + "b" * 128,
        private_key_pem="pem",
        signing_key_id="ha-1",
        trusted_key_ids=[a.key_id for a in admins],
        trusted_public_keys_hex={a.key_id: a.pub for a in admins},
    )
    await ks.async_add_island_key("phone-1", ISLAND_KEY, label="Pixel 8")
    hass.data.setdefault(DOMAIN, {})["keystore"] = ks
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "local_only": True,
            "cloud_ingest_enabled": False,
            "inverters": [{"inverter_id": "inv-1"}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _post(hass, body, **kwargs):
    return await SvitgridIslandSolarStringsView().post(_Req(hass, body, **kwargs))


def test_the_status_advertises_string_outage():
    assert "string_outage" in CAPABILITIES


async def test_a_signed_layout_is_kept(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(
        hass,
        admin.request(
            inverterId="inv-1",
            inverterName="Deye",
            strings=[{**TWO[0], "azimuth": 180, "tilt": 30, "mpptChannel": 1}, TWO[1]],
        ),
    )

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "stringCount": 2}
    layout = (await solar_strings_store(hass)).layout("inv-1")
    # Only what the rule reads is kept.
    assert layout == {"strings": TWO, "inverterName": "Deye"}


async def test_the_layout_survives_a_restart(hass: HomeAssistant, hass_storage):
    admin = _Admin()
    await _station(hass, admin)
    await _post(hass, admin.request(inverterId="inv-1", strings=TWO))
    await (await solar_strings_store(hass)).flush()

    again = SolarStringsStore(hass)
    await again.load()

    assert again.layout("inv-1") == {"strings": TWO, "inverterName": None}


async def test_an_empty_layout_clears_the_inverter(hass: HomeAssistant):
    """No panels, or the owner removed every string: nothing to check."""
    admin = _Admin()
    await _station(hass, admin)
    await _post(hass, admin.request(inverterId="inv-1", strings=TWO))

    resp = await _post(hass, admin.request(inverterId="inv-1", strings=[]))

    assert resp.status == 200
    assert (await solar_strings_store(hass)).layout("inv-1") is None


async def test_an_unsigned_layout_is_refused(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)
    body = admin.request(inverterId="inv-1", strings=TWO)
    del body["signature"]

    resp = await _post(hass, body)

    assert resp.status == 400
    assert (await solar_strings_store(hass)).layout("inv-1") is None


async def test_a_layout_by_an_untrusted_key_is_refused(hass: HomeAssistant):
    await _station(hass, _Admin())

    resp = await _post(hass, _Admin().request(inverterId="inv-1", strings=TWO))

    assert resp.status == 403
    assert (await solar_strings_store(hass)).layout("inv-1") is None


async def test_a_layout_without_the_island_key_is_unauthorized(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-1", strings=TWO), island_key=None)

    assert resp.status == 401


async def test_another_action_is_refused(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(action="bms_report", inverterId="inv-1", strings=TWO))

    assert resp.status == 400


@pytest.mark.parametrize(
    "fields",
    [
        {"strings": TWO},
        {"inverterId": "inv-1"},
        {"inverterId": "inv-1", "strings": "PV1"},
        {"inverterId": "inv-1", "strings": [None]},
        {"inverterId": "inv-1", "strings": [{"id": "PV1", "peakPowerWp": "3000"}]},
        {"inverterId": "inv-1", "strings": [{"id": "PV1", "peakPowerWp": True}]},
        {"inverterId": "inv-1", "strings": [{"id": "PV1", "peakPowerWp": -1}]},
        {"inverterId": "inv-1", "strings": [{"id": 1, "peakPowerWp": 3000}]},
        {"inverterId": "inv-1", "strings": [{"peakPowerWp": 3000}] * 17},
        {"inverterId": "inv-1", "inverterName": 5, "strings": TWO},
    ],
)
async def test_a_malformed_layout_is_bad_request(hass: HomeAssistant, fields):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(**fields))

    assert resp.status == 400
    assert (await solar_strings_store(hass)).layout("inv-1") is None


async def test_a_string_without_id_or_peak_power_is_kept_as_the_cloud_keeps_it(
    hass: HomeAssistant,
):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-1", strings=[{"id": "PV1"}, {}]))

    assert resp.status == 200
    assert (await solar_strings_store(hass)).layout("inv-1")["strings"] == [{"id": "PV1"}, {}]


async def test_a_layout_for_an_inverter_this_station_does_not_serve_is_refused(
    hass: HomeAssistant,
):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-9", strings=TWO))

    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_inverter"}


async def test_a_corrupt_stored_file_reads_as_no_layout(hass: HomeAssistant, hass_storage):
    hass_storage["svitgrid_solar_strings"] = {
        "version": 1,
        "key": "svitgrid_solar_strings",
        "data": {"inverters": {"inv-1": {"strings": "PV1"}, "inv-2": 7}},
    }
    store = SolarStringsStore(hass)
    await store.load()

    assert store.layout("inv-1") is None
    assert store.layout("inv-2") is None
