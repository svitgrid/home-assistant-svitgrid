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
import time
from datetime import UTC, datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .command_auth import verify_signed_command
from .const import DOMAIN
from .island_auth import island_key_present_and_valid, island_request_authorized

_DEDUPE_TTL_S = 300  # 5 minutes


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


def register_views(hass: HomeAssistant, store) -> None:
    for view in (SvitgridLiveView(store), SvitgridTodayView(store),
                 SvitgridHistoryView(store), SvitgridSyncStatusView(store),
                 SvitgridHealthView(store), SvitgridCommandsView()):
        hass.http.register_view(view)
