import pytest

from custom_components.svitgrid.http_views import (
    SvitgridHistoryView,
    SvitgridLiveView,
    SvitgridSyncStatusView,
    _today,
)


class _FakeStore:
    def __init__(self):
        self.history_args = None

    async def live_snapshot(self):
        return [{"inverterId": "inv-1", "ts": "2026-06-24T10:00:00Z",
                 "payload": {"pvPower": 2.0}}]

    async def sync_status(self):
        return {"counts": {"sent": 3, "pending": 1}, "last_sent_ts": "2026-06-24T10:00:00Z"}

    async def today_summary(self, day):
        return [{"inverterId": "inv-1", "sample_count": 3, "peaks": {}, "energy": {}}]

    async def history_range(self, inverter_id, start, end):
        self.history_args = (inverter_id, start, end)
        return [{"day": "2026-06-23", "sample_count": 5, "avgs": {}, "peaks": {}, "energy": {}}]


class _FakeRequest:
    def __init__(self, app, query=None):
        self.app = {"hass": app}
        self.query = query if query is not None else {}


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
    request = _FakeRequest(hass, query={
        "inverter_id": "inv-9",
        "start": "2026-06-20",
        "end": "2026-06-22",
    })
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
