"""Read-only HTTP API for the local readings store (Sub-project 1).

The Sub-project 2 panel consumes these endpoints.  Each view authorizes on an
authenticated HA session OR a valid ``X-Island-Key`` header so the mobile app
can read over the LAN without a full HA session.

Auth logic (``_BaseView._authorize``):
- ``requires_auth = False`` removes HA's automatic middleware gate.
- ``_authorize`` fetches the island key from the keystore (stored at
  ``hass.data["svitgrid"]["keystore"]``) then delegates to
  ``island_request_authorized``, which accepts either an authenticated HA
  session *or* a matching ``X-Island-Key`` header.
"""
from __future__ import annotations

from datetime import UTC, datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .island_auth import island_request_authorized


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class _BaseView(HomeAssistantView):
    # Disable HA's automatic session-only gate; we enforce auth ourselves.
    requires_auth = False

    def __init__(self, store) -> None:
        self._store = store

    async def _authorize(self, request) -> bool:
        """Return True iff the request is authorised (HA session OR island key).

        The keystore is looked up from ``hass.data[DOMAIN]["keystore"]``.  If
        the keystore is absent (e.g. integration not yet fully set up), the
        island key path is disabled (``island_key=None``) and only an
        authenticated HA session grants access.
        """
        hass = request.app["hass"]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        island_key: str | None = (
            await keystore.async_get_island_key() if keystore is not None else None
        )
        return island_request_authorized(request, island_key)


class SvitgridLiveView(_BaseView):
    url = "/api/svitgrid/live"
    name = "api:svitgrid:live"

    async def get(self, request):
        if not await self._authorize(request):
            return web.Response(status=401)
        return self.json({"inverters": await self._store.live_snapshot()})


class SvitgridTodayView(_BaseView):
    url = "/api/svitgrid/today"
    name = "api:svitgrid:today"

    async def get(self, request):
        if not await self._authorize(request):
            return web.Response(status=401)
        day = _today()
        return self.json({"day": day, "inverters": await self._store.today_summary(day)})


class SvitgridHistoryView(_BaseView):
    url = "/api/svitgrid/history"
    name = "api:svitgrid:history"

    async def get(self, request):
        if not await self._authorize(request):
            return web.Response(status=401)
        q = request.query
        inverter_id = q.get("inverter_id", "")
        if q.get("granularity") == "hourly":
            day = q.get("day", _today())
            return self.json({
                "inverter_id": inverter_id,
                "hours": await self._store.hourly_range(inverter_id, day),
            })
        start = q.get("start", _today())
        end = q.get("end", _today())
        return self.json({
            "inverter_id": inverter_id,
            "days": await self._store.history_range(inverter_id, start, end),
        })


class SvitgridSyncStatusView(_BaseView):
    url = "/api/svitgrid/sync-status"
    name = "api:svitgrid:sync-status"

    async def get(self, request):
        if not await self._authorize(request):
            return web.Response(status=401)
        return self.json(await self._store.sync_status())


class SvitgridHealthView(_BaseView):
    url = "/api/svitgrid/health"
    name = "api:svitgrid:health"

    async def get(self, request):
        if not await self._authorize(request):
            return web.Response(status=401)
        return self.json(await self._store.get_lifecycle())


def register_views(hass: HomeAssistant, store) -> None:
    for view in (SvitgridLiveView(store), SvitgridTodayView(store),
                 SvitgridHistoryView(store), SvitgridSyncStatusView(store),
                 SvitgridHealthView(store)):
        hass.http.register_view(view)
