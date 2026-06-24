import pytest

from custom_components.svitgrid.http_views import (
    SvitgridLiveView, SvitgridSyncStatusView,
)


class _FakeStore:
    async def live_snapshot(self):
        return [{"inverterId": "inv-1", "ts": "2026-06-24T10:00:00Z",
                 "payload": {"pvPower": 2.0}}]
    async def sync_status(self):
        return {"counts": {"sent": 3, "pending": 1}, "last_sent_ts": "2026-06-24T10:00:00Z"}


class _FakeRequest:
    def __init__(self, app):
        self.app = {"hass": app}
        self.query = {}


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
