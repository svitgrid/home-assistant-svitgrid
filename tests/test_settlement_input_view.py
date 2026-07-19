"""Tests for SvitgridSettlementInputView — GET /api/svitgrid/settlement-input.

Endpoint returns LocalDateHourBucket[] (per-hour import/export energy,
household-local bucketed) for a requested inverter+month. Wire contract must
match the cloud settlement's LocalDateHourBucket{localDate, hourOfDay,
importKwh, exportKwh} (services/api/src/tariffs/hourly-aggregation.ts:73-78).

Spec: docs/superpowers/plans/2026-07-02-island-hourly-energy.md, Task 2.
"""

from __future__ import annotations

import json

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridSettlementInputView, _today

ISLAND_KEY = "test-island-key-settlement"


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_http_views_island_auth.py)
# ---------------------------------------------------------------------------


class _FakeKeystore:
    def __init__(self, island_key: str | None) -> None:
        self._island_key = island_key

    async def async_get_island_key(self) -> str | None:
        return self._island_key

    async def async_get_island_keys(self) -> list[str]:
        return [self._island_key] if self._island_key else []


class _FakeHeaders(dict):
    """Case-insensitive header dict matching aiohttp CIMultiDictProxy semantics."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeRequest:
    def __init__(
        self,
        hass_obj,
        *,
        island_key_header: str | None = None,
        authenticated: bool = False,
        query: dict | None = None,
    ) -> None:
        self.app = {"hass": hass_obj}
        self.query = query or {}
        self._data: dict = {"ha_authenticated": authenticated}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header

    def get(self, key, default=None):  # noqa: D102
        return self._data.get(key, default)

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]


def _install_keystore(hass, island_key: str | None = ISLAND_KEY) -> None:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["keystore"] = _FakeKeystore(island_key)


def _hourly_row(hour_start, import_energy=None, export_energy=None):
    """A row shaped like reading_store's month_hourly_range_live output."""
    return {
        "hour_start": hour_start,
        "sample_count": 1,
        "avgs": {},
        "peaks": {},
        "energy": {
            "dailyGridImportEnergy": import_energy,
            "dailyGridExportEnergy": export_energy,
        },
    }


class _FakeStore:
    def __init__(self, hourly_rows):
        self._hourly_rows = hourly_rows
        self.args = None

    async def month_hourly_range_live(self, inverter_id, month, tz_name=None):
        self.args = (inverter_id, month)
        return self._hourly_rows


# ---------------------------------------------------------------------------
# Happy path: shape + per-hour deltas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_input_returns_local_date_hour_buckets(hass):
    _install_keystore(hass)
    hass.config.time_zone = "UTC"
    rows = [
        _hourly_row("2026-07-02T00:00:00Z", import_energy=1.0, export_energy=0.2),
        _hourly_row("2026-07-02T01:00:00Z", import_energy=2.5, export_energy=0.2),
    ]
    store = _FakeStore(rows)
    view = SvitgridSettlementInputView(store)
    request = _FakeRequest(
        hass,
        island_key_header=ISLAND_KEY,
        query={"inverter_id": "inv-1", "month": "2026-07"},
    )
    resp = await view.get(request)
    assert resp.status == 200
    assert store.args == ("inv-1", "2026-07")

    body = json.loads(resp.body)
    assert body["inverter_id"] == "inv-1"
    buckets = body["buckets"]
    assert len(buckets) == 2

    b0 = next(b for b in buckets if b["hourOfDay"] == 0)
    b1 = next(b for b in buckets if b["hourOfDay"] == 1)
    assert set(b0.keys()) == {"localDate", "hourOfDay", "importKwh", "exportKwh"}
    assert b0["localDate"] == "2026-07-02"
    assert b0["importKwh"] == 1.0
    assert b0["exportKwh"] == 0.2
    assert b1["localDate"] == "2026-07-02"
    # import differences against hour0's cum: 2.5 - 1.0 = 1.5
    assert b1["importKwh"] == 1.5
    # export unchanged hour-over-hour: 0.2 - 0.2 = 0.0
    assert b1["exportKwh"] == 0.0


# ---------------------------------------------------------------------------
# Local-date bucketing reflects the CONFIGURED tz, not raw UTC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_input_uses_configured_local_tz(hass):
    _install_keystore(hass)
    hass.config.time_zone = "Europe/Kyiv"
    # UTC 22:00 on 2026-07-01 -> Europe/Kyiv (UTC+3 in July, EEST) local
    # 2026-07-02T01:00 -- crosses into the next calendar day.
    rows = [
        _hourly_row("2026-07-01T22:00:00Z", import_energy=4.0, export_energy=0.0),
    ]
    store = _FakeStore(rows)
    view = SvitgridSettlementInputView(store)
    request = _FakeRequest(
        hass,
        island_key_header=ISLAND_KEY,
        query={"inverter_id": "inv-1", "month": "2026-07"},
    )
    resp = await view.get(request)
    assert resp.status == 200
    body = json.loads(resp.body)
    buckets = body["buckets"]
    assert len(buckets) == 1
    assert buckets[0]["localDate"] == "2026-07-02"
    assert buckets[0]["hourOfDay"] == 1
    assert buckets[0]["importKwh"] == 4.0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_input_no_key_no_session_returns_401(hass):
    _install_keystore(hass)
    store = _FakeStore([])
    view = SvitgridSettlementInputView(store)
    request = _FakeRequest(hass, query={"inverter_id": "inv-1", "month": "2026-07"})
    resp = await view.get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_settlement_input_authenticated_session_no_key_returns_200(hass):
    _install_keystore(hass)
    hass.config.time_zone = "UTC"
    store = _FakeStore([])
    view = SvitgridSettlementInputView(store)
    request = _FakeRequest(
        hass, authenticated=True, query={"inverter_id": "inv-1", "month": "2026-07"}
    )
    resp = await view.get(request)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_settlement_input_wrong_island_key_returns_401(hass):
    _install_keystore(hass)
    store = _FakeStore([])
    view = SvitgridSettlementInputView(store)
    request = _FakeRequest(
        hass,
        island_key_header="totally-wrong-key",
        query={"inverter_id": "inv-1", "month": "2026-07"},
    )
    resp = await view.get(request)
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_input_defaults_month_to_current_when_missing(hass):
    _install_keystore(hass)
    hass.config.time_zone = "UTC"
    store = _FakeStore([])
    view = SvitgridSettlementInputView(store)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, query={"inverter_id": "inv-1"})
    resp = await view.get(request)
    assert resp.status == 200
    assert store.args[1] == _today()[:7]


# ---------------------------------------------------------------------------
# Malformed month -> 400 (the real _month_bounds raises ValueError, which the
# endpoint maps to HTTP 400 rather than a 500)
# ---------------------------------------------------------------------------


class _RaisingStore:
    """Store whose month fetch raises ValueError for a malformed month, exactly
    like the real ReadingStore._month_bounds does."""

    async def month_hourly_range_live(self, inverter_id, month, tz_name=None):
        raise ValueError(f"malformed month: {month!r}")


@pytest.mark.asyncio
async def test_settlement_input_malformed_month_returns_400(hass):
    _install_keystore(hass)
    hass.config.time_zone = "UTC"
    view = SvitgridSettlementInputView(_RaisingStore())
    request = _FakeRequest(
        hass,
        island_key_header=ISLAND_KEY,
        query={"inverter_id": "inv-1", "month": "foo"},
    )
    resp = await view.get(request)
    assert resp.status == 400
