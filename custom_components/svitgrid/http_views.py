"""Read-only HTTP API for the local readings store (Sub-project 1) and
the signed-command endpoint (island mode SP1, Task 5).

The Sub-project 2 panel consumes the read endpoints.  Each read view
authorizes on an authenticated HA session OR a valid ``X-Island-Key``
header so the mobile app can read over the LAN without a full HA session.

The control endpoint (``SvitgridCommandsView``) uses the island key ONLY
(no HA session bypass) and additionally requires a valid admin ECDSA
signature over the command payload.

Auth logic (``_BaseView._authorize``):
- ``requires_auth = False`` removes HA's automatic middleware gate.
- ``_authorize`` fetches the island key from the keystore (stored at
  ``hass.data["svitgrid"]["keystore"]``) then delegates to
  ``island_request_authorized``, which accepts either an authenticated HA
  session *or* a matching ``X-Island-Key`` header.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .command_auth import verify_signed_command
from .const import DOMAIN
from .hourly_energy import per_hour_deltas, to_local_hour_rows
from .island_auth import island_key_present_and_valid, island_request_authorized

_LOGGER = logging.getLogger(__name__)

_DEDUPE_TTL_S = 300  # 5 minutes


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


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
                "hours": await self._store.hourly_range_live(inverter_id, day),
            })
        if q.get("granularity") == "5min":
            # Fine-grained (5-minute) buckets for the Day charts, computed live
            # from readings_raw (14-day retention). Same wire shape as hourly.
            day = q.get("day", _today())
            return self.json({
                "inverter_id": inverter_id,
                "hours": await self._store.five_min_range_live(inverter_id, day),
            })
        start = q.get("start", _today())
        end = q.get("end", _today())
        return self.json({
            "inverter_id": inverter_id,
            "days": await self._store.history_range_live(inverter_id, start, end),
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


class SvitgridCommandsView(HomeAssistantView):
    """POST /api/svitgrid/commands — island-key + admin-signature → WriteExecutor.

    Auth: island key ONLY (no HA session bypass).
    Body: {command, payload, signingKeyId, signedEventData, signature, commandId?}.
    Replay protection: in-memory TTL dict keyed on commandId (5-min window).
    Dispatch: executors aggregated from all config entries in hass.data[DOMAIN].
    """

    url = "/api/svitgrid/commands"
    name = "api:svitgrid:commands"
    requires_auth = False

    def __init__(self) -> None:
        # commandId → monotonic timestamp of first dispatch
        self._seen_commands: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_executors(self, hass) -> dict:
        """Aggregate executors_by_inverter from all config entries.

        Each config entry stores its data at hass.data[DOMAIN][entry_id],
        which is a dict with an "executors_by_inverter" sub-dict.  We merge
        all entries so the endpoint works regardless of how many inverters /
        entries are configured.
        """
        domain_data = hass.data.get(DOMAIN, {})
        merged: dict = {}
        for value in domain_data.values():
            if isinstance(value, dict) and "executors_by_inverter" in value:
                merged.update(value["executors_by_inverter"])
        return merged

    def _prune_seen(self, now: float) -> None:
        """Lazily remove expired dedupe entries."""
        expired = [k for k, ts in self._seen_commands.items() if now - ts > _DEDUPE_TTL_S]
        for k in expired:
            del self._seen_commands[k]

    @staticmethod
    def _json_error(status: int, error: str, **extra) -> web.Response:
        body = json.dumps({"error": error, **extra})
        return web.Response(status=status, text=body, content_type="application/json")

    # ------------------------------------------------------------------
    # POST handler
    # ------------------------------------------------------------------

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]

        # --- Auth: island key ONLY ---
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        island_key: str | None = (
            await keystore.async_get_island_key() if keystore is not None else None
        )
        if not island_key_present_and_valid(request, island_key):
            return self._json_error(401, "unauthorized")

        # --- Parse body ---
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return self._json_error(400, "bad_request")

        command = body.get("command")
        payload = body.get("payload")
        signing_key_id = body.get("signingKeyId")
        signed_event_data = body.get("signedEventData")
        signature = body.get("signature")
        command_id: str | None = body.get("commandId")

        # Required: command, payload (may be {}), signingKeyId, signedEventData, signature
        if not command or payload is None or not signing_key_id or signed_event_data is None or not signature:
            return self._json_error(400, "bad_request")

        # --- Verify admin signature ---
        keystore_state = await keystore.load() if keystore is not None else None
        trusted_public_keys_hex: dict[str, str] = (
            keystore_state.trusted_public_keys_hex if keystore_state is not None else {}
        )
        if not verify_signed_command(trusted_public_keys_hex, signing_key_id, signed_event_data, signature):
            return self._json_error(403, "signature_invalid")

        # --- Binding: ensure top-level command+payload match what was signed ---
        if not isinstance(signed_event_data, dict):
            return self._json_error(400, "bad_request")
        signed_command = signed_event_data.get("command")
        signed_payload = signed_event_data.get("payload")
        if not signed_command or not isinstance(signed_command, str) or not isinstance(signed_payload, dict):
            return self._json_error(400, "bad_request")
        if command != signed_command or payload != signed_payload:
            return self._json_error(403, "command_mismatch")

        # --- Replay protection ---
        now = time.monotonic()
        if command_id is not None:
            self._prune_seen(now)
            if command_id in self._seen_commands:
                return self.json({"ok": True, "deduped": True})

        # --- Dispatch using the signed values (authoritative source) ---
        inverter_id = signed_payload.get("inverterId")
        executors = self._get_executors(hass)
        executor = executors.get(inverter_id)

        if executor is None:
            return self._json_error(404, "unknown_inverter")

        try:
            result = await executor.dispatch(signed_command, signed_payload)
        except NotImplementedError:
            return self._json_error(422, "unsupported")
        except Exception as exc:  # noqa: BLE001
            return self._json_error(502, "executor_error", detail=str(exc))

        # Record commandId on success
        if command_id is not None:
            self._seen_commands[command_id] = now

        return self.json({"ok": True, "result": result})


class _IslandEventViewMixin:
    """Shared helpers for event views to avoid duplication."""

    @staticmethod
    def _json_error(status: int, error: str, **extra) -> web.Response:
        body = json.dumps({"error": error, **extra})
        return web.Response(status=status, text=body, content_type="application/json")

    @staticmethod
    async def _get_keystore(hass):
        return hass.data.get(DOMAIN, {}).get("keystore")

    @staticmethod
    async def _get_event_store(hass):
        return hass.data.get(DOMAIN, {}).get("event_store")


class SvitgridEventsView(_IslandEventViewMixin, HomeAssistantView):
    """GET/POST /api/svitgrid/events — island-key read, signed-write for calendar events.

    Auth:
    - GET: island key OR authenticated HA session.
    - POST: island key ONLY + admin ECDSA signature over the event body.

    POST body: {event, signingKeyId, signedEventData, signature}.
    Binding: ``event`` (top-level) must equal ``signedEventData`` exactly.
    ``signedEventData`` must be a dict; non-dict is rejected with 400.
    The ``signedEventData`` copy is authoritative — it is stored, not
    the top-level ``event``.

    Error shape mirrors SvitgridCommandsView: ``{"error": "<code>"}``.
    """

    url = "/api/svitgrid/events"
    name = "api:svitgrid:events"
    requires_auth = False

    async def get(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        keystore = await self._get_keystore(hass)
        island_key: str | None = (
            await keystore.async_get_island_key() if keystore is not None else None
        )
        if not island_request_authorized(request, island_key):
            return self._json_error(401, "unauthorized")

        event_store = await self._get_event_store(hass)
        if event_store is None:
            return self.json({"events": []})
        events = await event_store.async_list_events()
        return self.json({"events": events})

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]

        # --- Auth: island key ONLY ---
        keystore = await self._get_keystore(hass)
        island_key: str | None = (
            await keystore.async_get_island_key() if keystore is not None else None
        )
        if not island_key_present_and_valid(request, island_key):
            return self._json_error(401, "unauthorized")

        # --- Parse body ---
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return self._json_error(400, "bad_request")

        signing_key_id = body.get("signingKeyId")
        signed_event_data = body.get("signedEventData")
        signature = body.get("signature")
        event = body.get("event")

        if not signing_key_id or signed_event_data is None or not signature or event is None:
            return self._json_error(400, "bad_request")

        # --- Guard: signedEventData must be a dict (before signature verification) ---
        if not isinstance(signed_event_data, dict):
            return self._json_error(400, "bad_request")

        # --- Verify admin signature ---
        keystore_state = await keystore.load() if keystore is not None else None
        trusted_public_keys_hex: dict[str, str] = (
            keystore_state.trusted_public_keys_hex if keystore_state is not None else {}
        )
        if not verify_signed_command(
            trusted_public_keys_hex, signing_key_id, signed_event_data, signature
        ):
            return self._json_error(403, "signature_invalid")

        # --- Binding: top-level event must equal the signed copy ---
        if event != signed_event_data:
            return self._json_error(403, "event_mismatch")

        # --- Store the signed copy (authoritative source) ---
        event_store = await self._get_event_store(hass)
        if event_store is None:
            return self._json_error(503, "event_store_unavailable")

        await event_store.async_upsert_event(signed_event_data)
        return self.json({"ok": True, "event": signed_event_data})


class SvitgridEventDetailView(_IslandEventViewMixin, HomeAssistantView):
    """PUT/DELETE /api/svitgrid/events/{event_id} — island-key + admin signature.

    Auth: island key ONLY (no HA session bypass) + admin ECDSA signature.

    PUT body: {event, signingKeyId, signedEventData, signature}.
    Binding: ``event`` must equal ``signedEventData`` exactly AND
    ``signedEventData["id"]`` must match the URL ``event_id``.
    ``signedEventData`` must be a dict; non-dict is rejected with 400.
    ``signedEventData`` is stored (authoritative source).

    DELETE body: {signingKeyId, signedEventData, signature}.
    ``signedEventData`` must contain ``event_id`` matching the URL segment.

    Error shape mirrors SvitgridCommandsView: ``{"error": "<code>"}``.
    """

    url = "/api/svitgrid/events/{event_id}"
    name = "api:svitgrid:event_detail"
    requires_auth = False

    async def _check_island_key(self, request, hass) -> tuple[bool, object]:
        """Return (authorized, keystore). Checks island key ONLY (no session bypass)."""
        keystore = await self._get_keystore(hass)
        island_key: str | None = (
            await keystore.async_get_island_key() if keystore is not None else None
        )
        return island_key_present_and_valid(request, island_key), keystore

    async def _verify_signature(self, keystore, signing_key_id, signed_event_data, signature) -> bool:
        keystore_state = await keystore.load() if keystore is not None else None
        trusted_public_keys_hex: dict[str, str] = (
            keystore_state.trusted_public_keys_hex if keystore_state is not None else {}
        )
        return verify_signed_command(
            trusted_public_keys_hex, signing_key_id, signed_event_data, signature
        )

    async def put(self, request, event_id: str) -> web.Response:  # noqa: D102
        hass = request.app["hass"]

        authorized, keystore = await self._check_island_key(request, hass)
        if not authorized:
            return self._json_error(401, "unauthorized")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return self._json_error(400, "bad_request")

        signing_key_id = body.get("signingKeyId")
        signed_event_data = body.get("signedEventData")
        signature = body.get("signature")
        event = body.get("event")

        if not signing_key_id or signed_event_data is None or not signature or event is None:
            return self._json_error(400, "bad_request")

        # --- Guard: signedEventData must be a dict (before signature verification) ---
        if not isinstance(signed_event_data, dict):
            return self._json_error(400, "bad_request")

        if not await self._verify_signature(keystore, signing_key_id, signed_event_data, signature):
            return self._json_error(403, "signature_invalid")

        # --- Binding: top-level event must equal the signed copy ---
        if event != signed_event_data:
            return self._json_error(403, "event_mismatch")

        # --- Binding: signed event id must match URL event_id ---
        if signed_event_data.get("id") != event_id:
            return self._json_error(403, "event_mismatch")

        event_store = await self._get_event_store(hass)
        if event_store is None:
            return self._json_error(503, "event_store_unavailable")

        await event_store.async_upsert_event(signed_event_data)
        return self.json({"ok": True, "event": signed_event_data})

    async def delete(self, request, event_id: str) -> web.Response:  # noqa: D102
        hass = request.app["hass"]

        authorized, keystore = await self._check_island_key(request, hass)
        if not authorized:
            return self._json_error(401, "unauthorized")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return self._json_error(400, "bad_request")

        signing_key_id = body.get("signingKeyId")
        signed_event_data = body.get("signedEventData")
        signature = body.get("signature")

        if not signing_key_id or signed_event_data is None or not signature:
            return self._json_error(400, "bad_request")

        if not await self._verify_signature(keystore, signing_key_id, signed_event_data, signature):
            return self._json_error(403, "signature_invalid")

        # --- Binding: signed event_id must match URL event_id ---
        signed_id = signed_event_data.get("event_id") if isinstance(signed_event_data, dict) else None
        if signed_id != event_id:
            return self._json_error(403, "event_mismatch")

        event_store = await self._get_event_store(hass)
        if event_store is None:
            return self._json_error(503, "event_store_unavailable")

        deleted = await event_store.async_delete_event(event_id)
        return self.json({"ok": True, "deleted": deleted})


_CADENCE_PRESETS = {5, 15, 30, 60, 300}


class SvitgridCadenceView(_BaseView):
    """GET/PUT /api/svitgrid/cadence — island-key-authed harvest cadence control.

    GET returns ``{"intervalSeconds": int}`` for the current cadence.
    PUT ``{"intervalSeconds": int}`` validates against ``_CADENCE_PRESETS``,
    updates the shared ``Cadence`` holder, and persists via
    ``hass.config_entries.async_update_entry``.

    Auth: island key OR authenticated HA session (via ``_BaseView._authorize``).
    Returns 404 if the cadence holder is absent (integration not fully set up).
    """

    url = "/api/svitgrid/cadence"
    name = "api:svitgrid:cadence"

    def _cadence(self, request):
        return request.app["hass"].data.get(DOMAIN, {}).get("cadence")

    async def get(self, request):  # noqa: D102
        if not await self._authorize(request):
            return web.Response(status=401)
        cadence = self._cadence(request)
        if cadence is None:
            return web.Response(status=404)
        return self.json({"intervalSeconds": int(cadence.interval_s)})

    async def put(self, request):  # noqa: D102
        if not await self._authorize(request):
            return web.Response(status=401)
        try:
            body = await request.json()
            seconds = int(body["intervalSeconds"])
        except (ValueError, KeyError, TypeError):
            return web.Response(status=400)
        if seconds not in _CADENCE_PRESETS:
            return web.Response(status=400)
        hass = request.app["hass"]
        cadence = self._cadence(request)
        if cadence is None:
            return web.Response(status=404)
        cadence.interval_s = seconds
        entry_id = hass.data.get(DOMAIN, {}).get("cadence_entry_id")
        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is not None:
            # The in-memory cadence holder is already updated above and the
            # harvest loop reads it live every tick, so this update needs NO
            # entry reload. Flag it so the update listener (_async_reload_entry)
            # skips the reload — otherwise every cadence change would reload the
            # entry and re-run setup (which historically bricked the harvest
            # loop on the panel/view re-register path).
            hass.data.setdefault(DOMAIN, {})["_cadence_only_update"] = True
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "harvest_interval_seconds": seconds}
            )
        return self.json({"intervalSeconds": seconds})


class SvitgridSettlementInputView(_BaseView):
    """GET /api/svitgrid/settlement-input — per-hour import/export energy,
    household-local bucketed, for financial settlement calculations.

    Query params: ``inverter_id`` (required by the caller; defaults to "" if
    omitted), ``month`` ('YYYY-MM', defaults to the current UTC month).

    Response: ``{"inverter_id", "buckets": [{"localDate", "hourOfDay",
    "importKwh", "exportKwh"}, ...]}`` — the wire shape matches the cloud
    settlement's ``LocalDateHourBucket`` (``hourOfDay``, not ``hour``; see
    ``services/api/src/tariffs/hourly-aggregation.ts``).

    Flow: fetch the month's UTC hourly energy rows (sealed + today-live,
    via ``store.month_hourly_range_live``) → bucket to household-local
    date/hour (``to_local_hour_rows``, using ``hass.config.time_zone``) →
    compute per-hour deltas from the cumulative counters
    (``per_hour_deltas``).

    Auth: island key OR authenticated HA session (``_BaseView._authorize``).
    """

    url = "/api/svitgrid/settlement-input"
    name = "api:svitgrid:settlement-input"

    async def get(self, request):
        if not await self._authorize(request):
            return web.Response(status=401)
        q = request.query
        inverter_id = q.get("inverter_id", "")
        month = q.get("month", _current_month())
        tz_name = request.app["hass"].config.time_zone

        try:
            hourly_rows = await self._store.month_hourly_range_live(inverter_id, month)
        except ValueError:
            # Malformed month (e.g. "foo") — _month_bounds can't parse it.
            return web.Response(status=400)
        local_rows = to_local_hour_rows(hourly_rows, tz_name)
        deltas = per_hour_deltas(local_rows)
        buckets = [
            {
                "localDate": r["local_date"],
                "hourOfDay": r["hour"],
                "importKwh": r["importKwh"],
                "exportKwh": r["exportKwh"],
            }
            for r in deltas
        ]
        return self.json({"inverter_id": inverter_id, "buckets": buckets})


def register_views(hass: HomeAssistant, store) -> None:
    for view in (
        SvitgridLiveView(store),
        SvitgridTodayView(store),
        SvitgridHistoryView(store),
        SvitgridSyncStatusView(store),
        SvitgridHealthView(store),
        SvitgridCommandsView(),
        SvitgridEventsView(),
        SvitgridEventDetailView(),
        SvitgridCadenceView(store),
        SvitgridSettlementInputView(store),
    ):
        # View routes are GLOBAL to hass.http and PERSIST across config-entry
        # reloads, but the _views_registered guard in hass.data[DOMAIN] is
        # cleared on unload. On a reload we re-enter here with the guard gone
        # and the routes still present, so aiohttp raises RuntimeError
        # ("Added route will never be executed, method GET is already
        # registered"). Swallow that — the existing route already serves.
        try:
            hass.http.register_view(view)
        except RuntimeError as err:
            if "already registered" not in str(err):
                raise
            _LOGGER.debug(
                "Reusing already-registered view %s: %s",
                type(view).__name__, err,
            )
