"""Read-only HTTP API for the local readings store (Sub-project 1).

The Sub-project 2 panel consumes these endpoints. All views require HA auth.
"""
from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class _BaseView(HomeAssistantView):
    requires_auth = True

    def __init__(self, store) -> None:
        self._store = store


class SvitgridLiveView(_BaseView):
    url = "/api/svitgrid/live"
    name = "api:svitgrid:live"

    async def get(self, request):
        return self.json({"inverters": await self._store.live_snapshot()})


class SvitgridTodayView(_BaseView):
    url = "/api/svitgrid/today"
    name = "api:svitgrid:today"

    async def get(self, request):
        day = _today()
        return self.json({"day": day, "inverters": await self._store.today_summary(day)})


class SvitgridHistoryView(_BaseView):
    url = "/api/svitgrid/history"
    name = "api:svitgrid:history"

    async def get(self, request):
        q = request.query
        inverter_id = q.get("inverter_id", "")
        start = q.get("start", _today())
        end = q.get("end", _today())
        return self.json({
            "inverter_id": inverter_id,
            "days": await self._store.history_range(inverter_id, start, end),
        })


class SvitgridSyncStatusView(_BaseView):
    url = "/api/svitgrid/sync-status"
    name = "api:svitgrid:sync_status"

    async def get(self, request):
        return self.json(await self._store.sync_status())


def register_views(hass: HomeAssistant, store) -> None:
    for view in (SvitgridLiveView(store), SvitgridTodayView(store),
                 SvitgridHistoryView(store), SvitgridSyncStatusView(store)):
        hass.http.register_view(view)
