import pytest

from custom_components.svitgrid.http_views import (
    SvitgridHealthView,
    SvitgridHistoryView,
    SvitgridLiveView,
    SvitgridSyncStatusView,
    _today,
)


class _FakeStore:
    def __init__(self):
        self.history_args = None
        self.hourly_args = None

    async def live_snapshot(self):
        return [{"inverterId": "inv-1", "ts": "2026-06-24T10:00:00Z", "payload": {"pvPower": 2.0}}]

    async def sync_status(self):
        return {"counts": {"sent": 3, "pending": 1}, "last_sent_ts": "2026-06-24T10:00:00Z"}

    async def today_summary(self, day, tz_name=None):
        return [{"inverterId": "inv-1", "sample_count": 3, "peaks": {}, "energy": {}}]

    async def history_range(self, inverter_id, start, end, tz_name=None):
        self.history_args = (inverter_id, start, end)
        return [{"day": "2026-06-23", "sample_count": 5, "avgs": {}, "peaks": {}, "energy": {}}]

    async def history_range_live(self, inverter_id, start, end, tz_name=None):
        # The daily branch now routes through the live path (sealed prior + today live).
        self.history_args = (inverter_id, start, end)
        return [{"day": "2026-06-23", "sample_count": 5, "avgs": {}, "peaks": {}, "energy": {}}]

    async def hourly_range(self, inverter_id, day, tz_name=None):
        self.hourly_args = (inverter_id, day)
        return [
            {
                "hour": "2026-06-20T09:00:00Z",
                "sample_count": 10,
                "avgs": {},
                "peaks": {},
                "energy": {},
            },
            {
                "hour": "2026-06-20T10:00:00Z",
                "sample_count": 12,
                "avgs": {},
                "peaks": {},
                "energy": {},
            },
        ]

    async def hourly_range_live(self, inverter_id, day, tz_name=None):
        # The history view's hourly branch now computes buckets live from raw.
        self.hourly_args = (inverter_id, day)
        return [
            {
                "hour": "2026-06-20T09:00:00Z",
                "sample_count": 10,
                "avgs": {},
                "peaks": {},
                "energy": {},
            },
            {
                "hour": "2026-06-20T10:00:00Z",
                "sample_count": 12,
                "avgs": {},
                "peaks": {},
                "energy": {},
            },
        ]

    async def five_min_range_live(self, inverter_id, day, tz_name=None):
        self.five_min_args = (inverter_id, day)
        return [
            {
                "hour": "2026-06-20T09:00:00Z",
                "sample_count": 3,
                "avgs": {},
                "peaks": {},
                "energy": {},
            },
            {
                "hour": "2026-06-20T09:05:00Z",
                "sample_count": 4,
                "avgs": {},
                "peaks": {},
                "energy": {},
            },
        ]


class _FakeRequest:
    def __init__(self, app, query=None):
        self.app = {"hass": app}
        self.query = query if query is not None else {}
        # Simulate an authenticated HA session so _authorize passes.
        self._data: dict = {"ha_authenticated": True}
        self.headers: dict = {}  # No X-Island-Key header

    def get(self, key, default=None):  # noqa: D102
        return self._data.get(key, default)

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]


@pytest.mark.asyncio
async def test_live_view_returns_snapshot(hass):
    view = SvitgridLiveView(_FakeStore())
    resp = await view.get(_FakeRequest(hass))
    # HomeAssistantView.json returns an aiohttp Response with a JSON body.
    assert resp.status == 200
    assert b"inv-1" in resp.body


@pytest.mark.asyncio
async def test_sync_status_view_returns_counts(hass):
    view = SvitgridSyncStatusView(_FakeStore())
    resp = await view.get(_FakeRequest(hass))
    assert resp.status == 200
    assert b"last_sent_ts" in resp.body


@pytest.mark.asyncio
async def test_history_view_passes_query_params(hass):
    store = _FakeStore()
    view = SvitgridHistoryView(store)
    request = _FakeRequest(
        hass,
        query={
            "inverter_id": "inv-9",
            "start": "2026-06-20",
            "end": "2026-06-22",
        },
    )
    resp = await view.get(request)
    assert resp.status == 200
    assert store.history_args == ("inv-9", "2026-06-20", "2026-06-22")
    assert b"2026-06-23" in resp.body


@pytest.mark.asyncio
async def test_history_view_defaults_to_today_when_params_missing(hass):
    store = _FakeStore()
    view = SvitgridHistoryView(store)
    request = _FakeRequest(hass, query={})
    resp = await view.get(request)
    assert resp.status == 200
    today = _today()
    assert store.history_args[1] == store.history_args[2]
    assert store.history_args[1] == today


@pytest.mark.asyncio
async def test_health_view_returns_lifecycle(hass):
    class _S:
        async def get_lifecycle(self):
            return {"state": "deprovisioned", "reason": "revoked", "since": "2026-06-25T10:00:00Z"}

    view = SvitgridHealthView(_S())
    resp = await view.get(_FakeRequest(hass))
    assert resp.status == 200
    assert b"deprovisioned" in resp.body


@pytest.mark.asyncio
async def test_history_view_hourly_granularity_returns_hours(hass):
    store = _FakeStore()
    view = SvitgridHistoryView(store)
    request = _FakeRequest(
        hass,
        query={
            "inverter_id": "inv-9",
            "granularity": "hourly",
            "day": "2026-06-20",
        },
    )
    resp = await view.get(request)
    assert resp.status == 200
    assert store.hourly_args == ("inv-9", "2026-06-20")
    assert b"hours" in resp.body
    assert b"2026-06-20T09:00:00Z" in resp.body
    # Must NOT have called history_range
    assert store.history_args is None


@pytest.mark.asyncio
async def test_history_view_hourly_defaults_day_to_today(hass):
    store = _FakeStore()
    view = SvitgridHistoryView(store)
    request = _FakeRequest(hass, query={"granularity": "hourly", "inverter_id": "inv-1"})
    resp = await view.get(request)
    assert resp.status == 200
    today = _today()
    assert store.hourly_args == ("inv-1", today)
    assert b"hours" in resp.body


@pytest.mark.asyncio
async def test_history_view_5min_granularity_returns_fine_buckets(hass):
    store = _FakeStore()
    view = SvitgridHistoryView(store)
    request = _FakeRequest(
        hass,
        query={
            "inverter_id": "inv-9",
            "granularity": "5min",
            "day": "2026-06-20",
        },
    )
    resp = await view.get(request)
    assert resp.status == 200
    assert store.five_min_args == ("inv-9", "2026-06-20")
    # Same {hours: [...]} wire shape as the hourly path (mobile reuses it).
    assert b"hours" in resp.body
    assert b"2026-06-20T09:05:00Z" in resp.body
    # Did NOT fall through to the hourly or daily branch.
    assert store.hourly_args is None
    assert store.history_args is None


@pytest.mark.asyncio
async def test_history_view_daily_path_unchanged_with_granularity_absent(hass):
    """Plain ?start=&end= (no granularity) still returns {days: ...} unchanged."""
    store = _FakeStore()
    view = SvitgridHistoryView(store)
    request = _FakeRequest(
        hass,
        query={
            "inverter_id": "inv-9",
            "start": "2026-06-20",
            "end": "2026-06-22",
        },
    )
    resp = await view.get(request)
    assert resp.status == 200
    assert store.history_args == ("inv-9", "2026-06-20", "2026-06-22")
    assert b"days" in resp.body
    # hourly_range must not have been called
    assert store.hourly_args is None
