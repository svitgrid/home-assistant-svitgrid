"""Battery packs over the LAN for the app on an edge island (plan Task 44).

The box reports its packs to Home Assistant on the ingest batch's `bms`
(`edge_api.py`, `bms_packs.py`); these views serve them to the app, island
key only, shaped like the cloud's:

- `GET /api/svitgrid/bms/packs`: `/households/:id/bms-packs`, `packs` and
  `boards`. A pack's board is the box that reports it. Each board also
  carries the box's latest report as `bms`, as the cloud keeps it on
  `edgeDevices/{id}.bms`, so the app has the live readings beside the roster.
- `GET /api/svitgrid/bms/packs/{packId}/history?from=&to=`:
  `/bms-packs/:packId/history` (epoch ms, at most 8 days).
- `GET /api/svitgrid/bms/report?from=&to=`: `/bms-packs/report`
  (Reports › Батареї, local dates, at most 366 days), with no subscription
  gate on an island (spec 28).

While no box is adopted every view answers 404 `{"error":"no_edge_boxes"}`
before any auth check, so an install without one sees no change.
"""

from __future__ import annotations

from aiohttp import web

from .bms_packs import (
    HISTORY_MAX_RANGE_MS,
    PACK_ID_RE,
    REPORT_MAX_RANGE_DAYS,
    report_range_error,
)
from .edge_api import _entry_state as _edge_entry_state
from .island_lan import _island_key_ok, _json_error
from .zigbee_lan import _edge_boxes, _no_edge_boxes, _ZigbeeView


def _pack_view(pack: dict, box_id: str, last_ok: int | None, roster_error=None) -> dict:
    """`packView`: one pack as `GET …/bms-packs` lists it. The roster fields
    the island does not keep yet (Task 45) are null."""
    return {
        "packId": pack["id"],
        "assignedEdgeId": box_id,
        "driver": pack.get("driver"),
        "driverSource": pack.get("driverSource"),
        "driverSetAt": None,
        "suggestedDriver": pack.get("suggestedDriver"),
        "addrType": pack.get("addrType"),
        "label": pack.get("label"),
        "nominalMah": pack.get("nominalMah"),
        "adoptedAt": pack.get("adoptedAt"),
        "lastOkAt": last_ok,
        "rosterError": roster_error,
        "move": None,
    }


def _public_state(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "receivedAtMs"}


class _BmsView(_ZigbeeView):
    async def _packs(self, request):
        """The pack store, or a refusal: 404 `no_edge_boxes` before any auth,
        then 401 without the island key."""
        hass = request.app["hass"]
        if not await _edge_boxes(hass):
            return _no_edge_boxes()
        if not await _island_key_ok(request):
            return _json_error(401, "unauthorized")
        packs = (_edge_entry_state(hass) or {}).get("bms_packs")
        if packs is None:
            return _json_error(503, "unavailable")
        return packs


class BmsPacksView(_BmsView):
    """GET /api/svitgrid/bms/packs — every pack the boxes report, ordered by
    `adoptedAt`, then id; a pack a box refused to adopt is listed with its
    `rosterError`."""

    url = "/api/svitgrid/bms/packs"
    name = "api:svitgrid:bms:packs"

    async def get(self, request) -> web.Response:  # noqa: D102
        packs = await self._packs(request)
        if isinstance(packs, web.Response):
            return packs
        states = await packs.async_states()
        last_ok = await packs.async_last_ok()
        listed: dict[str, dict] = {}
        for box_id, state in states.items():
            errors = {e.get("packId"): e.get("rosterError") for e in state.get("rosterErrors", [])}
            for pack in state.get("packs", []):
                listed.setdefault(
                    pack["id"],
                    _pack_view(pack, box_id, last_ok.get(pack["id"]), errors.get(pack["id"])),
                )
            for pack_id, error in errors.items():
                if isinstance(pack_id, str) and pack_id not in listed:
                    listed[pack_id] = _pack_view({"id": pack_id}, box_id, None, error)
        ordered = sorted(
            listed.values(),
            key=lambda p: (
                p["adoptedAt"] if p["adoptedAt"] is not None else float("inf"),
                p["packId"],
            ),
        )
        boards = {
            box_id: {
                "reportIntervalMs": None,
                "lastReportAt": state.get("updatedAt"),
                "bms": _public_state(state),
            }
            for box_id, state in sorted(states.items())
        }
        return web.json_response({"packs": ordered, "boards": boards})


class BmsPackHistoryView(_BmsView):
    """GET /api/svitgrid/bms/packs/{pack_id}/history?from=&to= — the pack's
    5-minute samples: `{packId, from, to, cells, points}`. Up to 36 h carries
    every cell; up to 8 days only the spread."""

    url = "/api/svitgrid/bms/packs/{pack_id}/history"
    name = "api:svitgrid:bms:pack:history"

    async def get(self, request, pack_id: str) -> web.Response:  # noqa: D102
        packs = await self._packs(request)
        if isinstance(packs, web.Response):
            return packs
        try:
            from_ms = int(request.query.get("from", ""))
            to_ms = int(request.query.get("to", ""))
        except ValueError:
            from_ms = to_ms = -1
        if not PACK_ID_RE.match(pack_id) or from_ms < 0 or to_ms <= from_ms:
            return _json_error(
                400, "bad_request", detail="packId, from and to (epoch ms, from < to)"
            )
        if to_ms - from_ms > HISTORY_MAX_RANGE_MS:
            return web.json_response(
                {"error": "range_too_long", "maxRangeMs": HISTORY_MAX_RANGE_MS}, status=400
            )
        history = await packs.async_pack_history(pack_id, from_ms, to_ms)
        return web.json_response({"packId": pack_id, "from": from_ms, "to": to_ms, **history})


class BmsReportView(_BmsView):
    """GET /api/svitgrid/bms/report?from=&to= — every pack with one summary
    per local day of `from`..`to` (YYYY-MM-DD, inclusive): `{from, to,
    timezone, packs: [{packId, title, driver, cellCount?, nominalAh?,
    days}]}`. No subscription gate on an island."""

    url = "/api/svitgrid/bms/report"
    name = "api:svitgrid:bms:report"

    async def get(self, request) -> web.Response:  # noqa: D102
        packs = await self._packs(request)
        if isinstance(packs, web.Response):
            return packs
        from_date, to_date = request.query.get("from"), request.query.get("to")
        error = report_range_error(from_date, to_date)
        if error == "range_too_long":
            return web.json_response(
                {"error": "range_too_long", "maxRangeDays": REPORT_MAX_RANGE_DAYS}, status=400
            )
        if error is not None:
            return web.json_response({"error": error}, status=400)
        tz_name = request.app["hass"].config.time_zone
        return web.json_response(await packs.async_report(from_date, to_date, tz_name))


BMS_LAN_VIEWS = (BmsPacksView, BmsPackHistoryView, BmsReportView)
