"""The hourly island status report (svitgrid D-1719, API side D-1715).

A true island sends no readings to the cloud, so the cloud cannot count it on
the landing map or the fleet dashboard. Once an hour its add-on reports a small
status per inverter to POST /api/v1/island-report with its cloud API key:
latest solar power, solar energy of the last hour, and battery charge. Its own
inverters report as host `ha`, the inverters of an adopted edge box as `edge`.

Every answer, refusals included, carries `{nextReportAfterSec, stop}`. The
add-on obeys it and keeps it across restarts. `stop` ends reporting until the
next start, and the next start checks again no sooner than a day later.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import SvitgridApiClient
from custom_components.svitgrid.island_report import (
    DEFAULT_INTERVAL_S,
    STOP_RECHECK_S,
    build_report,
    pv_energy_kwh,
    report_once,
    run_island_report_loop,
    seconds_until_due,
)
from custom_components.svitgrid.reading_store import ReadingStore

NOW = 1_762_876_800.0  # 2025-11-11T16:00:00Z


def _iso(epoch_s: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch_s, UTC).isoformat().replace("+00:00", "Z")


def _reading(inverter_id: str, at: float, **fields) -> dict:
    return {"inverterId": inverter_id, "timestamp": _iso(at), **fields}


def _store(tmp_path, readings: list[dict]) -> ReadingStore:
    store = ReadingStore(None, str(tmp_path / "readings.db"))
    for r in readings:
        store._append_sync(r)
    return store


class _SyncStore:
    """ReadingStore's async surface over its sync core, with no hass."""

    def __init__(self, store: ReadingStore) -> None:
        self._s = store

    async def island_report_rows(self, inverter_id, since_iso):
        return self._s._island_report_rows_sync(inverter_id, since_iso)

    async def get_island_report_state(self):
        return self._s._get_island_report_state_sync()

    async def set_island_report_state(self, state):
        self._s._set_island_report_state_sync(state)


# ── what is reported ────────────────────────────────────────────────────────


def test_energy_is_the_daily_counter_rise_over_the_hour():
    baseline = {"dailyPvEnergy": 10.0}
    window = [{"dailyPvEnergy": 10.5}, {"dailyPvEnergy": 11.2}, {"dailyPvEnergy": 12.0}]
    assert pv_energy_kwh(baseline, [(NOW - 1800, p) for p in window]) == pytest.approx(2.0)


def test_energy_across_the_midnight_reset_counts_from_zero():
    baseline = {"dailyPvEnergy": 30.0}
    window = [{"dailyPvEnergy": 30.4}, {"dailyPvEnergy": 0.1}, {"dailyPvEnergy": 0.3}]
    assert pv_energy_kwh(baseline, [(NOW - 1800, p) for p in window]) == pytest.approx(0.7)


def test_energy_without_a_counter_integrates_solar_power():
    # 3 kW held for the half hour between two samples is 1.5 kWh.
    rows = [(NOW - 1800, {"pvPower": 3000}), (NOW, {"pvPower": 3000})]
    assert pv_energy_kwh(None, rows) == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_report_lists_own_inverters_as_ha_and_edge_box_inverters_as_edge(tmp_path):
    store = _store(
        tmp_path,
        [
            _reading("inv-own", NOW - 3700, pvPower=0, dailyPvEnergy=4.0, batterySoc=60),
            _reading("inv-own", NOW - 1800, pvPower=2000, dailyPvEnergy=5.0, batterySoc=70),
            _reading("inv-own", NOW - 60, pvPower=3120, dailyPvEnergy=6.84, batterySoc=76.4),
            _reading("inv-edge", NOW - 3800, pvPower=0, dailyPvEnergy=1.0),
            _reading("inv-edge", NOW - 120, pvPower=900, dailyPvEnergy=1.5),
        ],
    )
    report = await build_report(
        _SyncStore(store), [("inv-own", "ha"), ("inv-edge", "edge")], now_s=NOW
    )
    assert report == [
        {
            "inverterId": "inv-own",
            "host": "ha",
            "pvPowerW": 3120,
            "pvEnergyKwhHour": pytest.approx(2.84),
            "batterySoc": 76,
        },
        {
            "inverterId": "inv-edge",
            "host": "edge",
            "pvPowerW": 900,
            "pvEnergyKwhHour": pytest.approx(0.5),
            "batterySoc": None,
        },
    ]


@pytest.mark.asyncio
async def test_an_inverter_with_no_reading_in_the_last_hour_is_left_out(tmp_path):
    """Reporting a silent inverter would count it as an active station."""
    store = _store(tmp_path, [_reading("inv-own", NOW - 7200, pvPower=500)])
    assert await build_report(_SyncStore(store), [("inv-own", "ha")], now_s=NOW) == []


# ── the directive ───────────────────────────────────────────────────────────


def test_a_first_start_reports_at_once():
    assert seconds_until_due(None, NOW) == 0


def test_the_persisted_interval_holds_across_a_restart():
    state = {"lastAttemptAt": NOW - 600, "nextReportAfterSec": 3600, "stop": False}
    assert seconds_until_due(state, NOW) == pytest.approx(3000)


def test_a_persisted_stop_waits_a_day_after_the_last_check():
    state = {"lastAttemptAt": NOW - 3600, "nextReportAfterSec": 3600, "stop": True}
    assert seconds_until_due(state, NOW) == pytest.approx(STOP_RECHECK_S - 3600)


def _client(status: int, body):
    client = MagicMock()
    client.report_island_status = AsyncMock(return_value=(status, body))
    return client


@pytest.mark.asyncio
async def test_report_once_persists_the_directive_of_a_refusal(tmp_path):
    store = _store(tmp_path, [_reading("inv-own", NOW - 60, pvPower=100)])
    client = _client(429, {"error": "island_report_too_soon", "nextReportAfterSec": 3600})
    state = await report_once(
        store=_SyncStore(store),
        api_client=client,
        api_key="cloud-api-key",
        inverters=[("inv-own", "ha")],
        now_s=NOW,
    )
    assert state == {"lastAttemptAt": NOW, "nextReportAfterSec": 3600, "stop": False}
    assert store._get_island_report_state_sync() == state
    client.report_island_status.assert_awaited_once()
    assert client.report_island_status.call_args.args[0] == "cloud-api-key"


@pytest.mark.asyncio
async def test_a_failed_call_retries_after_the_default_interval(tmp_path):
    store = _store(tmp_path, [_reading("inv-own", NOW - 60, pvPower=100)])
    client = MagicMock()
    client.report_island_status = AsyncMock(side_effect=OSError("offline"))
    state = await report_once(
        store=_SyncStore(store),
        api_client=client,
        api_key="k",
        inverters=[("inv-own", "ha")],
        now_s=NOW,
    )
    assert state["nextReportAfterSec"] == DEFAULT_INTERVAL_S
    assert state["stop"] is False


@pytest.mark.asyncio
async def test_a_tiny_interval_from_the_server_does_not_hammer_it(tmp_path):
    store = _store(tmp_path, [_reading("inv-own", NOW - 60, pvPower=100)])
    client = _client(200, {"accepted": 1, "nextReportAfterSec": 1, "stop": False})
    state = await report_once(
        store=_SyncStore(store),
        api_client=client,
        api_key="k",
        inverters=[("inv-own", "ha")],
        now_s=NOW,
    )
    assert state["nextReportAfterSec"] >= 15 * 60


@pytest.mark.asyncio
async def test_stop_ends_the_loop_until_the_next_start(tmp_path):
    store = _store(tmp_path, [_reading("inv-own", NOW - 60, pvPower=100)])
    client = _client(200, {"accepted": 0, "nextReportAfterSec": 86400, "stop": True})
    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)

    await run_island_report_loop(
        store=_SyncStore(store),
        api_client=client,
        api_key="k",
        inverters=lambda: [("inv-own", "ha")],
        now=lambda: NOW,
        sleep=_sleep,
    )
    client.report_island_status.assert_awaited_once()
    assert store._get_island_report_state_sync()["stop"] is True


@pytest.mark.asyncio
async def test_a_restart_inside_the_interval_does_not_call_early(tmp_path):
    store = _store(tmp_path, [_reading("inv-own", NOW - 60, pvPower=100)])
    store._set_island_report_state_sync(
        {"lastAttemptAt": NOW - 600, "nextReportAfterSec": 3600, "stop": False}
    )
    client = _client(200, {"accepted": 1, "nextReportAfterSec": 3600, "stop": True})
    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)

    await run_island_report_loop(
        store=_SyncStore(store),
        api_client=client,
        api_key="k",
        inverters=lambda: [("inv-own", "ha")],
        now=lambda: NOW,
        sleep=_sleep,
    )
    assert sleeps[0] == pytest.approx(3000)


# ── the wire ────────────────────────────────────────────────────────────────


def _session(status: int, body):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.text = AsyncMock(return_value="")
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    session.post = MagicMock(return_value=resp)
    return session


@pytest.mark.asyncio
async def test_client_posts_the_list_with_the_cloud_api_key():
    session = _session(200, {"accepted": 1, "nextReportAfterSec": 3600, "stop": False})
    client = SvitgridApiClient(session, api_base="https://api.example/")
    entries = [
        {
            "inverterId": "inv-own",
            "host": "ha",
            "pvPowerW": 1,
            "pvEnergyKwhHour": 0,
            "batterySoc": None,
        }
    ]
    status, body = await client.report_island_status("cloud-api-key", entries)
    assert status == 200
    assert body["nextReportAfterSec"] == 3600
    url = session.post.call_args.args[0]
    assert url == "https://api.example/api/v1/island-report"
    assert session.post.call_args.kwargs["headers"] == {"x-api-key": "cloud-api-key"}
    assert session.post.call_args.kwargs["json"] == {"inverters": entries}


@pytest.mark.asyncio
async def test_client_returns_the_directive_of_a_refusal():
    session = _session(429, {"error": "island_report_too_soon", "nextReportAfterSec": 3600})
    client = SvitgridApiClient(session, api_base="https://api.example")
    status, body = await client.report_island_status("k", [])
    assert status == 429
    assert body["nextReportAfterSec"] == 3600
