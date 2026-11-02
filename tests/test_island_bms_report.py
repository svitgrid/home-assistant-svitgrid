"""A phone-read BMS report reaches a true island over the LAN (svitgrid#1329).

A true island hears packs only from an edge box, and a phone otherwise posts
its report to the cloud, where nothing on the island sees it. The phone now
posts it to `POST /api/svitgrid/island/bms`, signed as the other true-island
writes are, and Home Assistant keeps it as the cloud keeps
`inverters/{id}.bms` (`writeInverterBmsState`): merged by pack id, an empty
report never deleting, each pack it read stamped with `readAtMs`. The
battery-temperature condition then counts it, a box's copy of a pack winning
over a phone's, and only while it is fresh (`bmsTemperatureFromSources`).
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.bms_packs import BmsPackStore
from custom_components.svitgrid.bms_temperature import bms_temperature_from_states
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.island_lan import (
    SvitgridIslandBmsReportView,
    SvitgridIslandStatusView,
)
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload

ISLAND_KEY = "p" * 43
NOW_MS = 1_760_000_000_000

PACK = {
    "id": "aaaaaaaaaa01",
    "driver": "jk02_32s",
    "result": "ok",
    "everRead": True,
    "ageMs": 20_000,
    "tempsC": [14, 13, 0, 0, 18],
}


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

    def request(self, **fields) -> dict:
        signed = {"action": "bms_report", "nonce": uuid.uuid4().hex, "ts": NOW_MS, **fields}
        return {
            "signingKeyId": self.key_id,
            "signedData": signed,
            "signature": sign_payload(signed, self.priv),
        }


@pytest.fixture(autouse=True)
def _pinned_clock():
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


@pytest.fixture
def phone_store(hass, tmp_path) -> BmsPackStore:
    store = BmsPackStore(os.path.join(tmp_path, "bms_packs.db"))
    hass.data.setdefault(DOMAIN, {})["bms_report_store"] = store
    return store


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


async def _post(hass, body):
    return await SvitgridIslandBmsReportView().post(_Req(hass, body))


async def test_a_signed_report_is_stored_with_the_time_each_pack_was_read(
    hass: HomeAssistant, phone_store
):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-1", report={"packs": [PACK]}))

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "packCount": 1}
    states = await phone_store.async_phone_states()
    [pack] = states["inv-1"]["packs"]
    assert pack["id"] == PACK["id"]
    assert pack["readAtMs"] == NOW_MS - 20_000
    # The cloud's other BMS views read the boxes' states, never a phone's.
    assert await phone_store.async_states() == {}


async def test_an_unsigned_report_is_refused_and_not_stored(hass: HomeAssistant, phone_store):
    admin = _Admin()
    await _station(hass, admin)
    unsigned = admin.request(inverterId="inv-1", report={"packs": [PACK]})
    del unsigned["signature"]

    resp = await _post(hass, unsigned)

    assert resp.status == 400
    assert await phone_store.async_phone_states() == {}


async def test_a_report_by_an_untrusted_key_is_refused(hass: HomeAssistant, phone_store):
    await _station(hass, _Admin())

    resp = await _post(hass, _Admin().request(inverterId="inv-1", report={"packs": [PACK]}))

    assert resp.status == 403
    assert _body(resp) == {"error": "signature_invalid"}
    assert await phone_store.async_phone_states() == {}


async def test_a_report_without_the_island_key_is_unauthorized(hass: HomeAssistant, phone_store):
    admin = _Admin()
    await _station(hass, admin)
    body = admin.request(inverterId="inv-1", report={"packs": [PACK]})

    resp = await SvitgridIslandBmsReportView().post(_Req(hass, body, island_key=None))

    assert resp.status == 401


@pytest.mark.parametrize(
    "fields",
    [
        {"report": {"packs": [PACK]}},
        {"inverterId": "inv-1"},
        {"inverterId": "inv-1", "report": {"packs": [{**PACK, "id": "not-a-mac"}]}},
        {"inverterId": "inv-1", "report": {"packs": [PACK], "removed": "aaaaaaaaaa01"}},
    ],
)
async def test_a_malformed_report_is_bad_request(hass: HomeAssistant, phone_store, fields):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(**fields))

    assert resp.status == 400
    assert await phone_store.async_phone_states() == {}


async def test_a_report_for_an_inverter_this_station_does_not_serve_is_refused(
    hass: HomeAssistant, phone_store
):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-9", report={"packs": [PACK]}))

    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_inverter"}


async def test_reports_merge_by_pack_and_an_empty_one_keeps_them(hass: HomeAssistant, phone_store):
    admin = _Admin()
    await _station(hass, admin)
    other = {**PACK, "id": "bbbbbbbbbb02", "ageMs": 0}

    await _post(hass, admin.request(inverterId="inv-1", report={"packs": [PACK]}))
    await _post(hass, admin.request(inverterId="inv-1", report={"packs": [other]}))
    resp = await _post(hass, admin.request(inverterId="inv-1", report={"packs": []}))

    assert resp.status == 200
    packs = (await phone_store.async_phone_states())["inv-1"]["packs"]
    assert [p["id"] for p in packs] == ["aaaaaaaaaa01", "bbbbbbbbbb02"]

    await _post(
        hass,
        admin.request(inverterId="inv-1", report={"packs": [], "removed": ["aaaaaaaaaa01"]}),
    )
    packs = (await phone_store.async_phone_states())["inv-1"]["packs"]
    assert [p["id"] for p in packs] == ["bbbbbbbbbb02"]


async def test_a_phone_pack_counts_for_the_battery_temperature_while_fresh(
    hass: HomeAssistant, phone_store
):
    admin = _Admin()
    await _station(hass, admin)
    await _post(hass, admin.request(inverterId="inv-1", report={"packs": [PACK]}))
    states = await phone_store.async_phone_states()

    # Phantom zeros dropped, as for a box's pack.
    assert bms_temperature_from_states(states, NOW_MS) == 15
    # Read 20 s before NOW_MS; stale 15 minutes after that.
    assert bms_temperature_from_states(states, NOW_MS - 20_000 + 15 * 60_000 + 1) is None


async def test_the_scheduler_hears_the_phone_and_a_box_copy_wins(
    hass: HomeAssistant, phone_store, tmp_path
):
    from custom_components.svitgrid import _bms_states

    admin = _Admin()
    entry = await _station(hass, admin)
    await _post(hass, admin.request(inverterId="inv-1", report={"packs": [PACK]}))

    # No edge box: the phone's pack alone.
    hass.data[DOMAIN][entry.entry_id] = {"bms_packs": None}
    assert bms_temperature_from_states(await _bms_states(hass, entry.entry_id), NOW_MS) == 15

    # A box reads the same pack: its copy wins.
    boards = BmsPackStore(os.path.join(tmp_path, "boards.db"))
    await boards.async_store_report("box-1", {"packs": [{**PACK, "tempsC": [30, 30]}]})
    hass.data[DOMAIN][entry.entry_id] = {"bms_packs": boards}
    states = await _bms_states(hass, entry.entry_id)
    now = states["box-1"]["receivedAtMs"]
    assert bms_temperature_from_states(states, now) == 30


async def test_status_advertises_the_endpoint_and_the_condition(hass: HomeAssistant, phone_store):
    admin = _Admin()
    entry = await _station(hass, admin)
    hass.data[DOMAIN][entry.entry_id] = {}

    before = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert "bms_report_lan" in before["capabilities"]
    assert "conditionTypes" not in before["inverters"]["inv-1"]

    await _post(hass, admin.request(inverterId="inv-1", report={"packs": [PACK]}))

    after = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert after["inverters"]["inv-1"]["conditionTypes"] == ["bms_temperature"]


async def test_the_view_is_registered_with_the_others(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert "/api/svitgrid/island/bms" in urls
