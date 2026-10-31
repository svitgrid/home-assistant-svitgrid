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

- `POST /api/svitgrid/bms/import`: the labels and drivers the app copies
  from the cloud after the boxes bind (Task 48), owner-signed.
- `POST /api/svitgrid/edge/bms/command`: the app's pack commands (Task 45),
  island key only (spec 27), except `bms_forget`, which an owner signs. A
  roster change (adopt, forget, rename, move, driver) changes Home
  Assistant's desired set and sends it as `bms_sync` (`bms_roster.py`).

While no box is adopted every view answers 404 `{"error":"no_edge_boxes"}`
before any auth check, so an install without one sees no change.
"""

from __future__ import annotations

import time

from aiohttp import web

from . import bms_roster
from .bms_packs import (
    HISTORY_MAX_RANGE_MS,
    PACK_ID_RE,
    REPORT_MAX_RANGE_DAYS,
    report_range_error,
)
from .edge_api import _entry_state as _edge_entry_state
from .edge_api import _household_id
from .island_lan import _device_id, _island_key_ok, _json_error, signed_lan_request
from .zigbee_lan import _body, _edge_boxes, _no_edge_boxes, _ZigbeeView

ACTION_BMS_COMMAND = "edge_bms_command"
ACTION_BMS_IMPORT = "bms_import"

# Queued for the box as sent; a second pending copy supersedes the first
# (`DEDUP_EDGE_COMMANDS`, `routes/edge-devices.ts`).
DEDUP_BMS_COMMANDS = ("bms_scan", "bms_read_now", "bms_refresh")
BOX_BMS_COMMANDS = (*DEDUP_BMS_COMMANDS, "bms_capture")
# Change Home Assistant's desired roster, sent to the box as `bms_sync`.
ROSTER_BMS_COMMANDS = ("bms_adopt", "bms_rename", "bms_move", "bms_set_driver")
OWNER_BMS_COMMANDS = ("bms_forget",)
LABEL_MAX = 40
DRIVER_MAX = 32


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
        imported = await packs.async_imported()
        listed: dict[str, dict] = {}
        # A pack in a roster Home Assistant manages is listed as the roster
        # holds it, beside the box that should read it.
        for box_id, roster in (await packs.async_rosters()).items():
            reported = {p["id"]: p for p in states.get(box_id, {}).get("packs", [])}
            for entry in roster["packs"]:
                pack = {
                    **reported.get(entry["packId"], {}),
                    "id": entry["packId"],
                    "label": entry.get("label"),
                    "driver": entry.get("driver"),
                    "driverSource": entry.get("driverSource"),
                    "addrType": entry.get("addrType"),
                    "adoptedAt": entry.get("adoptedAt"),
                }
                listed[entry["packId"]] = _pack_view(pack, box_id, last_ok.get(entry["packId"]))
        for box_id, state in states.items():
            errors = {e.get("packId"): e.get("rosterError") for e in state.get("rosterErrors", [])}
            for pack in state.get("packs", []):
                pack = {**pack, **imported.get(pack["id"], {})}
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


def _pack_id(payload: dict) -> str | None:
    pack_id = payload.get("packId")
    return pack_id if isinstance(pack_id, str) and PACK_ID_RE.match(pack_id) else None


def _optional_str(payload: dict, key: str, limit: int) -> tuple[bool, str | None]:
    """`(ok, value)`: absent or null is `(True, None)`."""
    value = payload.get(key)
    if value is None:
        return True, None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False, None
    return True, value.strip()


class BmsCommandView(_BmsView):
    """POST /api/svitgrid/edge/bms/command — a battery-pack command for a box.

    `{deviceId, command, payload}` with the island key:

    - `bms_scan {durationS?}`, `bms_read_now`, `bms_refresh` are queued for
      the box, a newer one superseding a pending one; `bms_capture {packId}`
      is queued as sent.
    - `bms_adopt {packId, label?, driver?, addrType?}` adds the pack to the
      box's set (409 `PACK_READ_BY_OTHER_BOARD`, `BOARD_ROSTER_FULL`);
      `bms_rename {packId, label}`, `bms_set_driver {packId, driver}` (null
      lets the box identify it) and `bms_move {packId, toDeviceId}` change it.

    `bms_forget {packId}` is owner-only: a signed request, `signedData`:
    `{action: "edge_bms_command", deviceId, command: "bms_forget", payload,
    nonce, ts}`; with the island key alone it is 403 `owner_only`.

    Answers `{success, commandId, status, sent: [{edgeDeviceId, commandId}]}`,
    `commandId` the box's command (null for a rename, which the box does not
    hear: labels stay in Home Assistant).
    """

    url = "/api/svitgrid/edge/bms/command"
    name = "api:svitgrid:edge:bms:command"

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        boxes = await _edge_boxes(hass)
        if not boxes:
            return _no_edge_boxes()
        body = await _body(request)
        if body is not None and "signedData" in body:
            signed, error = await signed_lan_request(request, ACTION_BMS_COMMAND)
            if error is not None:
                return error
            command = signed.get("command")
            if command not in OWNER_BMS_COMMANDS:
                return _json_error(400, "bad_request", detail="unknown command")
            source = signed
        else:
            packs = await self._packs(request)
            if isinstance(packs, web.Response):
                return packs
            if body is None:
                return _json_error(400, "bad_request")
            command = body.get("command")
            if command in OWNER_BMS_COMMANDS:
                return _json_error(403, "owner_only")
            if command not in BOX_BMS_COMMANDS + ROSTER_BMS_COMMANDS:
                return _json_error(400, "bad_request", detail="unknown command")
            source = body
        box_id = _device_id(source)
        if box_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
        if box_id not in boxes:
            return _json_error(404, "unknown_box")
        packs = (_edge_entry_state(hass) or {}).get("bms_packs")
        queue = (_edge_entry_state(hass) or {}).get("edge_queue")
        if packs is None or queue is None:
            return _json_error(503, "unavailable")
        if command in BOX_BMS_COMMANDS:
            return await self._box_command(queue, box_id, command, payload)
        return await self._roster_command(hass, packs, queue, boxes, box_id, command, payload)

    async def _box_command(self, queue, box_id: str, command: str, payload: dict):
        if command == "bms_capture":
            pack_id = _pack_id(payload)
            if pack_id is None:
                return _json_error(400, "bad_request", detail="packId")
            payload = {"packId": pack_id}
        elif command == "bms_scan":
            duration = payload.get("durationS")
            if duration is not None and (type(duration) is not int or duration < 1):
                return _json_error(400, "bad_request", detail="durationS")
            payload = {"durationS": duration} if duration is not None else {}
        else:
            payload = {}
        if command in DEDUP_BMS_COMMANDS:
            await queue.async_supersede(box_id, command)
        command_id = await queue.async_enqueue(
            box_id, command, payload, signing=None, inverter_id=None, source="bms"
        )
        return _sent({box_id: command_id})

    async def _roster_command(self, hass, packs, queue, boxes, box_id, command, payload):
        pack_id = _pack_id(payload)
        if pack_id is None:
            return _json_error(400, "bad_request", detail="packId")
        rosters = await packs.async_rosters()
        states = await packs.async_states()
        held = {b: [p["packId"] for p in r["packs"]] for b, r in rosters.items()}
        for b, state in states.items():
            held.setdefault(b, [p["id"] for p in state.get("packs", [])])
        owner = next((b for b, ids in held.items() if pack_id in ids), None)

        if command == "bms_adopt":
            ok_label, label = _optional_str(payload, "label", LABEL_MAX)
            ok_driver, driver = _optional_str(payload, "driver", DRIVER_MAX)
            addr_type = payload.get("addrType")
            if not ok_label or not ok_driver or addr_type not in (None, 0, 1):
                return _json_error(400, "bad_request", detail="label, driver or addrType")
            if owner is not None and owner != box_id:
                return _json_error(409, "PACK_READ_BY_OTHER_BOARD", edgeDeviceId=owner)
            if owner is None and len(held.get(box_id, [])) >= bms_roster.MAX_PACKS_PER_BOX:
                return _json_error(409, "BOARD_ROSTER_FULL", maxPacks=bms_roster.MAX_PACKS_PER_BOX)
            entry = {
                "packId": pack_id,
                "label": label,
                "driver": driver,
                "driverSource": "owner" if driver else None,
                "addrType": addr_type,
                "adoptedAt": int(time.time()),
            }

            def change(rosters):
                packs_of_box = [p for p in rosters.get(box_id, []) if p["packId"] != pack_id]
                kept = next((p for p in rosters.get(box_id, []) if p["packId"] == pack_id), {})
                adopted = {**entry, "adoptedAt": kept.get("adoptedAt") or entry["adoptedAt"]}
                return {**rosters, box_id: [*packs_of_box, adopted]}

        else:
            if owner != box_id:
                return _json_error(404, "unknown_pack")
            if command == "bms_forget":

                def change(rosters):
                    return {
                        **rosters,
                        box_id: [p for p in rosters[box_id] if p["packId"] != pack_id],
                    }

            elif command == "bms_rename":
                ok, label = _optional_str(payload, "label", LABEL_MAX)
                if not ok:
                    return _json_error(400, "bad_request", detail="label")

                def change(rosters):
                    return {**rosters, box_id: _with(rosters[box_id], pack_id, label=label)}

            elif command == "bms_set_driver":
                ok, driver = _optional_str(payload, "driver", DRIVER_MAX)
                if not ok:
                    return _json_error(400, "bad_request", detail="driver")

                def change(rosters):
                    return {
                        **rosters,
                        box_id: _with(
                            rosters[box_id],
                            pack_id,
                            driver=driver,
                            driverSource="owner" if driver else None,
                        ),
                    }

            else:  # bms_move
                to_box = payload.get("toDeviceId")
                if not isinstance(to_box, str) or to_box not in boxes:
                    return _json_error(404, "unknown_box")
                if to_box != box_id and (len(held.get(to_box, [])) >= bms_roster.MAX_PACKS_PER_BOX):
                    return _json_error(
                        409, "BOARD_ROSTER_FULL", maxPacks=bms_roster.MAX_PACKS_PER_BOX
                    )

                def change(rosters):
                    pack = next(p for p in rosters[box_id] if p["packId"] == pack_id)
                    return {
                        **rosters,
                        box_id: [p for p in rosters[box_id] if p["packId"] != pack_id],
                        to_box: [
                            *[p for p in rosters.get(to_box, []) if p["packId"] != pack_id],
                            pack,
                        ],
                    }

        changed = await packs.async_change_rosters(change)
        if command == "bms_rename":
            # `bms_sync` carries no label: the box has nothing to hear.
            return _sent({})
        household = _household_id(hass)
        sent = {}
        for changed_box in changed:
            command_id = await bms_roster.async_deliver(
                packs, queue, household, changed_box, force=True
            )
            if command_id is not None:
                sent[changed_box] = command_id
        return _sent(sent)


class BmsImportView(_BmsView):
    """POST /api/svitgrid/bms/import — the labels and drivers of a box's
    packs, copied from the cloud after the box binds.

    Owner-signed, `signedData`: `{action: "bms_import", deviceId, packs:
    [{packId, label?, driver?}], nonce, ts}`; with the island key alone it is
    403 `owner_only`. Answers `{imported: [packId], pending: [packId],
    skipped: [{packId?, reason}]}`: a pack the box has not reported yet is
    kept for that box and applied when the box first reports it (`pending`);
    a malformed entry is skipped (`bad_request`).

    A box Home Assistant manages has its roster changed (a new driver is sent
    as `bms_sync`); one it does not keeps no roster, because a box with none
    is left alone (`bms_roster.async_converge`). Its packs show the copy over
    what the box reports, and its first roster change starts from it.
    """

    url = "/api/svitgrid/bms/import"
    name = "api:svitgrid:bms:import"

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        boxes = await _edge_boxes(hass)
        if not boxes:
            return _no_edge_boxes()
        if not await _island_key_ok(request):
            return _json_error(401, "unauthorized")
        body = await _body(request)
        if body is None or "signedData" not in body:
            return _json_error(403, "owner_only")
        signed, error = await signed_lan_request(request, ACTION_BMS_IMPORT)
        if error is not None:
            return error
        box_id = _device_id(signed)
        if box_id is None or not isinstance(signed.get("packs"), list):
            return _json_error(400, "bad_request", detail="deviceId and packs")
        if box_id not in boxes:
            return _json_error(404, "unknown_box")
        packs = (_edge_entry_state(hass) or {}).get("bms_packs")
        queue = (_edge_entry_state(hass) or {}).get("edge_queue")
        if packs is None or queue is None:
            return _json_error(503, "unavailable")

        roster = await packs.async_roster(box_id)
        if roster is not None:
            held = {p["packId"] for p in roster["packs"]}
        else:
            held = {p["id"] for p in ((await packs.async_state(box_id)) or {}).get("packs", [])}
        wanted: dict[str, dict] = {}
        pending: dict[str, dict] = {}
        skipped: list[dict] = []
        for item in signed["packs"]:
            item = item if isinstance(item, dict) else {}
            pack_id = _pack_id(item)
            ok_label, label = _optional_str(item, "label", LABEL_MAX)
            ok_driver, driver = _optional_str(item, "driver", DRIVER_MAX)
            if pack_id is None or not ok_label or not ok_driver:
                named = {"packId": item["packId"]} if isinstance(item.get("packId"), str) else {}
                skipped.append({**named, "reason": "bad_request"})
                continue
            fields = {}
            if label is not None:
                fields["label"] = label
            if driver is not None:
                fields["driver"] = driver
                fields["driverSource"] = "owner"
            (wanted if pack_id in held else pending)[pack_id] = fields

        await packs.async_import_pending(box_id, pending, settled=list(wanted))
        if roster is None:
            await packs.async_import(wanted)
        else:

            def change(rosters):
                return {
                    **rosters,
                    box_id: [{**p, **wanted.get(p["packId"], {})} for p in rosters[box_id]],
                }

            changed = (await packs.async_change_rosters(change)).get(box_id)
            # A label is not part of `bms_sync`: only a new driver is sent.
            if changed is not None and changed["n"] != roster["n"]:
                await bms_roster.async_deliver(
                    packs, queue, _household_id(hass), box_id, force=True
                )
        return web.json_response(
            {"imported": list(wanted), "pending": list(pending), "skipped": skipped}
        )


def _with(packs: list[dict], pack_id: str, **fields) -> list[dict]:
    return [{**p, **fields} if p["packId"] == pack_id else p for p in packs]


def _sent(sent: dict[str, str]) -> web.Response:
    """`{success, commandId, status, sent}`; `commandId` the first box's."""
    first = next(iter(sent.values()), None)
    return web.json_response(
        {
            "success": True,
            "commandId": first,
            "status": "pending" if first is not None else "executed",
            "sent": [{"edgeDeviceId": b, "commandId": c} for b, c in sent.items()],
        }
    )


BMS_LAN_VIEWS = (
    BmsPacksView,
    BmsPackHistoryView,
    BmsReportView,
    BmsCommandView,
    BmsImportView,
)
