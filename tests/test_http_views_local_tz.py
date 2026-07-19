"""The HTTP layer is where the household timezone enters the read path.

``hass.config.time_zone`` is the only place the household's wall clock is
known, so every day-scoped store call must carry it, and each intraday bucket
must ship the LOCAL hour it belongs to. The panel plots ``localHour``
directly; deriving it client-side from the UTC ``hour`` string is exactly the
bug this replaces.
"""

import json

import pytest

from custom_components.svitgrid.http_views import SvitgridHistoryView, SvitgridTodayView

KYIV = "Europe/Kyiv"  # UTC+3 in July


class _TzRecordingStore:
    """Records the tz each call receives and returns one UTC-stamped bucket."""

    def __init__(self):
        self.calls: dict[str, tuple] = {}

    async def hourly_range_live(self, inverter_id, day, tz_name=None):
        self.calls["hourly"] = (inverter_id, day, tz_name)
        return [
            {
                "hour": "2026-07-15T05:00:00Z",  # local 08:00 in Kyiv
                "sample_count": 10,
                "avgs": {"pvPower": 1900.0},
                "peaks": {},
                "energy": {},
            }
        ]

    async def five_min_range_live(self, inverter_id, day, tz_name=None):
        self.calls["five_min"] = (inverter_id, day, tz_name)
        return [
            {
                "hour": "2026-07-15T05:05:00Z",  # local 08:05 in Kyiv
                "sample_count": 3,
                "avgs": {},
                "peaks": {},
                "energy": {},
            }
        ]

    async def history_range_live(self, inverter_id, start, end, tz_name=None):
        self.calls["history"] = (inverter_id, start, end, tz_name)
        return [{"day": "2026-07-15", "sample_count": 5, "avgs": {}, "peaks": {}, "energy": {}}]

    async def today_summary(self, day, tz_name=None):
        self.calls["today"] = (day, tz_name)
        return [{"inverterId": "inv-1", "sample_count": 3, "peaks": {}, "energy": {}}]


class _FakeRequest:
    def __init__(self, hass, query=None):
        self.app = {"hass": hass}
        self.query = query if query is not None else {}
        self._data: dict = {"ha_authenticated": True}
        self.headers: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]


async def _get_json(view, request):
    resp = await view.get(request)
    assert resp.status == 200
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_hourly_buckets_carry_the_local_hour(hass):
    """The reported bug, at the wire boundary: a 05:00Z bucket must be
    labelled hour 8 for a Kyiv household, not 5."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 8
    assert body["hours"][0]["hour"] == "2026-07-15T05:00:00Z"  # UTC key preserved


@pytest.mark.asyncio
async def test_hourly_query_forwards_the_household_timezone(hass):
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    await SvitgridHistoryView(store).get(request)

    assert store.calls["hourly"] == ("", "2026-07-15", KYIV)


@pytest.mark.asyncio
async def test_five_min_buckets_carry_the_local_hour(hass):
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "5min", "day": "2026-07-15"})

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 8
    assert store.calls["five_min"] == ("", "2026-07-15", KYIV)


@pytest.mark.asyncio
async def test_daily_history_forwards_the_household_timezone(hass):
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"start": "2026-07-01", "end": "2026-07-31"})

    await SvitgridHistoryView(store).get(request)

    assert store.calls["history"] == ("", "2026-07-01", "2026-07-31", KYIV)


@pytest.mark.asyncio
async def test_today_view_uses_the_local_calendar_day(hass, monkeypatch):
    """At 2026-07-15T22:30Z it is already Jul 16 in Kyiv -- "today" must be
    Jul 16, or the household sees yesterday's summary for three hours every
    night."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()

    import custom_components.svitgrid.http_views as hv

    monkeypatch.setattr(hv, "_utc_now_iso", lambda: "2026-07-15T22:30:00Z")

    body = await _get_json(SvitgridTodayView(store), _FakeRequest(hass))

    assert body["day"] == "2026-07-16"
    assert store.calls["today"] == ("2026-07-16", KYIV)


@pytest.mark.asyncio
async def test_unset_timezone_degrades_to_utc(hass):
    """A household with no configured timezone keeps the old UTC behaviour
    rather than erroring."""
    hass.config.time_zone = None
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 5
