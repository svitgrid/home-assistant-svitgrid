"""The app hands Home Assistant each inverter's maintenance settings (svitgrid D-2163).

The cloud decides whether a battery maintenance charge is due from
`inverters/{id}.batteryMaintenance`: `intervalDays`, the last run, a forced
date and the grid fallback offset. Home Assistant never receives that
document, so a daily maintenance event copied to the island charged every
day. The app sends the settings to `POST /api/svitgrid/island/battery-maintenance`,
signed as the other true-island writes are, when the owner saves them and when
a station moves to Home Assistant. Home Assistant records each run's date
itself, and the status advertises `maintenance_charge`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.battery_maintenance import (
    BatteryMaintenanceStore,
    battery_maintenance_store,
    is_maintenance_due,
    valid_settings,
)
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.island_lan import (
    CAPABILITIES,
    SvitgridIslandBatteryMaintenanceView,
)
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload

ISLAND_KEY = "p" * 43
NOW_MS = 1_760_000_000_000
SETTINGS = {
    "enabled": True,
    "intervalDays": 21,
    "lastRunAt": "2026-10-01T09:00:00.000Z",
    "lastRunOutcome": "completed",
    "gridFallbackOffsetMinutes": 120,
    "fillStartTime": None,
    "drainEnabled": False,
    "drainStartTime": None,
    "drainEndTime": None,
    "drainTargetSocPercent": None,
    "forcedNextRunDate": None,
}

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "maintenance_due_cases.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_due_check_runs_the_clouds_cases(case):
    args = case["args"]
    due = is_maintenance_due(
        enabled=args["enabled"],
        interval_days=args["intervalDays"],
        last_run_at=args["lastRunAt"],
        today=datetime.fromisoformat(args["today"]),
        forced_next_run_date=args.get("forcedNextRunDate"),
        today_local_date_str=args.get("todayLocalDateStr"),
    )
    assert due is case["due"]


def test_settings_keep_the_clouds_fields():
    assert valid_settings({**SETTINGS, "extra": 1}) == SETTINGS


def test_settings_absent_optional_fields_read_as_none():
    minimal = {"enabled": True, "intervalDays": 14, "gridFallbackOffsetMinutes": 60}
    assert valid_settings(minimal) == {
        **SETTINGS,
        "intervalDays": 14,
        "gridFallbackOffsetMinutes": 60,
        "lastRunAt": None,
        "lastRunOutcome": None,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"intervalDays": 3},
        {"intervalDays": 91},
        {"intervalDays": True},
        {"enabled": "yes"},
        {"lastRunAt": "yesterday"},
        {"lastRunOutcome": "done"},
        {"gridFallbackOffsetMinutes": 400},
        {"fillStartTime": "25:00"},
        {"forcedNextRunDate": "2026-7-1"},
        {"drainTargetSocPercent": 99},
    ],
)
def test_settings_the_cloud_would_refuse_are_refused(bad):
    assert valid_settings({**SETTINGS, **bad}) is None


async def test_a_run_is_recorded_and_survives_a_restart(hass: HomeAssistant, hass_storage):
    store = await battery_maintenance_store(hass)
    await store.async_set("inv-1", {**SETTINGS, "forcedNextRunDate": "2026-10-15"})

    await store.async_record_run("inv-1", datetime(2026, 10, 15, 13, 0, tzinfo=UTC), "completed")
    await store.flush()
    again = BatteryMaintenanceStore(hass)
    await again.load()

    kept = again.settings("inv-1")
    assert kept["lastRunAt"] == "2026-10-15T13:00:00.000Z"
    assert kept["lastRunOutcome"] == "completed"
    # A completed run serves a forced date, as the cloud's stamp clears it.
    assert kept["forcedNextRunDate"] is None


async def test_an_incomplete_run_keeps_the_last_run_date(hass: HomeAssistant):
    store = await battery_maintenance_store(hass)
    await store.async_set("inv-1", {**SETTINGS, "forcedNextRunDate": "2026-10-15"})

    await store.async_record_run("inv-1", datetime(2026, 10, 15, 20, 0, tzinfo=UTC), "incomplete")

    kept = store.settings("inv-1")
    assert kept["lastRunAt"] == SETTINGS["lastRunAt"]
    assert kept["lastRunOutcome"] == "incomplete"
    assert kept["forcedNextRunDate"] == "2026-10-15"


async def test_a_resend_never_moves_the_last_run_back(hass: HomeAssistant):
    """The app sends what the cloud holds; on an island the cloud has an older date."""
    store = await battery_maintenance_store(hass)
    await store.async_set("inv-1", SETTINGS)
    await store.async_record_run("inv-1", datetime(2026, 10, 20, 13, 0, tzinfo=UTC), "completed")

    await store.async_set(
        "inv-1", {**SETTINGS, "intervalDays": 30, "forcedNextRunDate": "2026-10-20"}
    )

    kept = store.settings("inv-1")
    assert kept["intervalDays"] == 30
    assert kept["lastRunAt"] == "2026-10-20T13:00:00.000Z"
    assert kept["lastRunOutcome"] == "completed"
    # The forced date that run served comes back with the stale copy: dropped.
    assert kept["forcedNextRunDate"] is None


async def test_a_newer_last_run_from_the_app_wins(hass: HomeAssistant):
    """A skip from the app stamps today; it must hold the next run off."""
    store = await battery_maintenance_store(hass)
    await store.async_set("inv-1", SETTINGS)

    later = {**SETTINGS, "lastRunAt": "2026-10-22T08:00:00.000Z", "lastRunOutcome": "skipped"}
    await store.async_set("inv-1", later)

    assert store.settings("inv-1")["lastRunAt"] == "2026-10-22T08:00:00.000Z"
    assert store.settings("inv-1")["lastRunOutcome"] == "skipped"


async def test_a_future_forced_date_from_the_app_is_kept(hass: HomeAssistant):
    store = await battery_maintenance_store(hass)
    await store.async_set("inv-1", SETTINGS)
    await store.async_record_run("inv-1", datetime(2026, 10, 20, 13, 0, tzinfo=UTC), "completed")

    await store.async_set("inv-1", {**SETTINGS, "forcedNextRunDate": "2026-10-28"})

    assert store.settings("inv-1")["forcedNextRunDate"] == "2026-10-28"


async def test_a_corrupt_stored_file_reads_as_no_settings(hass: HomeAssistant, hass_storage):
    hass_storage["svitgrid_battery_maintenance"] = {
        "version": 1,
        "key": "svitgrid_battery_maintenance",
        "data": {"inverters": {"inv-1": {"intervalDays": "x"}, "inv-2": 7}},
    }
    store = BatteryMaintenanceStore(hass)
    await store.load()

    assert store.settings("inv-1") is None
    assert store.settings("inv-2") is None


# ─── POST /api/svitgrid/island/battery-maintenance ──────────────────────────


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

    def request(self, action="battery_maintenance", **fields) -> dict:
        signed = {"action": action, "nonce": uuid.uuid4().hex, "ts": NOW_MS, **fields}
        return {
            "signingKeyId": self.key_id,
            "signedData": signed,
            "signature": sign_payload(signed, self.priv),
        }


@pytest.fixture
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
    return await SvitgridIslandBatteryMaintenanceView().post(_Req(hass, body, **kwargs))


def test_the_status_advertises_maintenance_charge():
    assert "maintenance_charge" in CAPABILITIES


@pytest.mark.usefixtures("_pinned_clock")
async def test_signed_settings_are_kept(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-1", settings=SETTINGS))

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "lastRunAt": SETTINGS["lastRunAt"]}
    assert (await battery_maintenance_store(hass)).settings("inv-1") == SETTINGS


@pytest.mark.usefixtures("_pinned_clock")
async def test_null_settings_drop_the_inverters(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)
    await _post(hass, admin.request(inverterId="inv-1", settings=SETTINGS))

    resp = await _post(hass, admin.request(inverterId="inv-1", settings=None))

    assert resp.status == 200, _body(resp)
    assert (await battery_maintenance_store(hass)).settings("inv-1") is None


@pytest.mark.usefixtures("_pinned_clock")
async def test_malformed_settings_are_refused(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(
        hass, admin.request(inverterId="inv-1", settings={**SETTINGS, "intervalDays": 1})
    )

    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"
    assert (await battery_maintenance_store(hass)).settings("inv-1") is None


@pytest.mark.usefixtures("_pinned_clock")
async def test_another_action_is_refused(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(
        hass, admin.request(action="solar_strings", inverterId="inv-1", settings=SETTINGS)
    )

    assert resp.status == 400
    assert (await battery_maintenance_store(hass)).settings("inv-1") is None


@pytest.mark.usefixtures("_pinned_clock")
async def test_an_untrusted_signer_is_refused(hass: HomeAssistant):
    await _station(hass, _Admin())

    resp = await _post(hass, _Admin().request(inverterId="inv-1", settings=SETTINGS))

    assert resp.status == 403
    assert (await battery_maintenance_store(hass)).settings("inv-1") is None


@pytest.mark.usefixtures("_pinned_clock")
async def test_an_inverter_the_station_does_not_serve_is_refused(hass: HomeAssistant):
    admin = _Admin()
    await _station(hass, admin)

    resp = await _post(hass, admin.request(inverterId="inv-9", settings=SETTINGS))

    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_inverter"}
