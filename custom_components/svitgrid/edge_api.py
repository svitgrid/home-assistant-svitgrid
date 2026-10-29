"""The edge-facing server: the subset of the cloud edge API an edge box calls.

An edge box on an edge island reports to this integration instead of the
cloud. It keeps its protocol, so these views answer at the cloud's paths and
authenticate the box by its `X-API-Key`, which the keystore binds to an
adopted box (`KeystoreState.edge_boxes`).

Two rules hold on every view here:

- Nothing is reachable until a box is adopted. While the keystore holds no
  edge box, every view answers 404 `{"error":"no_edge_boxes"}` before any auth
  check. HA cannot unregister a view, so the gate lives in the view, and an
  install that never adopts a box sees no change.
- Never 410. A 410 on an edge path evicts the box and reboots it. An unknown
  key is 401 `{"error":"unauthorized"}`.

The paths only the cloud can serve (bootstrap, MQTT, firmware offers,
coredumps, Zigbee) get a fixed answer, 2xx wherever the box only needs to hear
that the call landed: its cloud-contact watchdog restarts it after two hours
without a 2xx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from dataclasses import replace
from datetime import UTC, datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .keystore import EdgeBoxEntry, KeystoreState
from .settings_read_plan import settings_snapshot_kind_for
from .settings_sync import store_edge_settings_snapshot

_LOGGER = logging.getLogger(__name__)


def _server_time() -> str:
    """Now, in the cloud's wire format: `YYYY-MM-DDTHH:MM:SS.fffZ`."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The largest body an edge view inflates from gzip. A settings body is a few
# kilobytes; anything past this is refused rather than held in memory.
MAX_INFLATED_BODY_BYTES = 256 * 1024

# The per-inverter keys of the cloud's `config` block that the box reads
# (`config_sync_models_from_block`, the reconcile in `cloud_uploader.c`).
_CONFIG_INVERTER_KEYS = (
    "ipAddress",
    "loggerSerial",
    "modelId",
    "protocolId",
    "phases",
    "hasBattery",
    "slaveId",
    "pollingInterval",
    "reportingInterval",
)


def build_edge_config(box: EdgeBoxEntry) -> dict:
    """The `config` block for `box`, in the cloud's keys.

    The box's reconcile prunes every local inverter the block does not list,
    so it lists every inverter the box holds, even one whose adopt details
    are missing. `configHash` is the first 16 hex digits of the SHA-256 of
    the block's canonical JSON (sorted keys, no spaces), without the hash.
    """
    inverter_ids = list(box.inverter_ids)
    inverter_ids += [i for i in box.inverters if i not in inverter_ids]
    inverters = []
    for inverter_id in inverter_ids:
        adopted = box.inverters.get(inverter_id) or {}
        inverters.append(
            {
                "inverterId": inverter_id,
                **{k: adopted[k] for k in _CONFIG_INVERTER_KEYS if adopted.get(k) is not None},
            }
        )
    block = {
        "inverters": inverters,
        "pollingInterval": 60000,
        "reportingInterval": 60000,
        "nonMeteringModels": [],
        "chargers": [],
        "relayChargers": [],
    }
    canonical = json.dumps(block, sort_keys=True, separators=(",", ":"))
    block["configHash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return block


async def _json_body(request):
    """The request's JSON body, or None when it is not JSON.

    The box gzips every body of 256 bytes or more
    (`HTTP_WORKER_COMPRESS_MIN_BYTES`) and aiohttp does not inflate a request
    body by itself.
    """
    if "gzip" not in (request.headers.get("Content-Encoding") or "").lower():
        try:
            return await request.json()
        except ValueError:
            return None
    inflater = zlib.decompressobj(wbits=31)
    try:
        raw = inflater.decompress(await request.read(), MAX_INFLATED_BODY_BYTES)
    except zlib.error:
        return None
    if inflater.unconsumed_tail:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _snapshot_from_settings_body(box: EdgeBoxEntry, body) -> tuple[dict | None, str | None]:
    """The settings snapshot a `sync-settings` body stores, or why not.

    The body is one of the firmware's five: the Deye register batch
    (`startRegister`, `registers`), or a brand body for LuxPower, MUST,
    SMG II or PI30, each with `inverterId` and `modelId`. It is stored as the
    add-on's own settings read stores it, so the settings view serves the two
    alike. The brand body is kept as the box sent it.
    """
    if not isinstance(body, dict):
        return None, "bad_body"
    inverter_id = body.get("inverterId")
    model_id = body.get("modelId")
    if not isinstance(inverter_id, str) or not isinstance(model_id, str):
        return None, "bad_body"
    if inverter_id not in box.inverter_ids and inverter_id not in box.inverters:
        return None, "unknown_inverter"
    adopted_model = (box.inverters.get(inverter_id) or {}).get("modelId")
    if adopted_model is not None and adopted_model != model_id:
        return None, "model_mismatch"
    kind = settings_snapshot_kind_for(model_id)
    if kind is None:
        return None, "nothing_to_configure"
    read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "deye":
        start_register = body.get("startRegister")
        registers = body.get("registers")
        if type(start_register) is not int or not isinstance(registers, list):
            return None, "bad_body"
        return {
            "inverter_id": inverter_id,
            "model_id": model_id,
            "start_register": start_register,
            "registers": list(registers),
            "read_at": read_at,
        }, None
    brand_body = {k: v for k, v in body.items() if k not in ("inverterId", "modelId", "readAt")}
    return {
        "inverter_id": inverter_id,
        "model_id": model_id,
        "kind": kind,
        "body": brand_body,
        "read_at": read_at,
    }, None


class _EdgeView(HomeAssistantView):
    """Base for the edge views: the no-boxes gate and the box's key."""

    requires_auth = False

    @staticmethod
    def _keystore(hass):
        return (hass.data.get(DOMAIN) or {}).get("keystore")

    async def _state(self, request) -> KeystoreState | web.Response:
        """The keystore state, or the 404 every edge view answers while no
        box is adopted. Runs before any auth check."""
        keystore = self._keystore(request.app["hass"])
        state = await keystore.load() if keystore is not None else None
        if state is None or not state.edge_boxes:
            return self.json({"error": "no_edge_boxes"}, status_code=404)
        return state

    async def _box(self, request) -> EdgeBoxEntry | web.Response:
        """The adopted box the request's `X-API-Key` is bound to, or a refusal.

        The pairing-code binding (`X-Pair-Code`) is Task 11; until then an
        unknown key is 401 with or without a code.
        """
        state = await self._state(request)
        if isinstance(state, web.Response):
            return state
        box = state.edge_box_for_api_key(request.headers.get("X-API-Key") or "")
        if box is None:
            return self.json({"error": "unauthorized"}, status_code=401)
        return box


class EdgeHealthView(_EdgeView):
    url = "/health"
    name = "svitgrid:edge:health"

    async def get(self, request):
        state = await self._state(request)
        if isinstance(state, web.Response):
            return state
        return self.json({"status": "ok", "timestamp": _server_time()})


class EdgeMeView(_EdgeView):
    url = "/api/v3/me"
    name = "svitgrid:edge:me"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        hass = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        return self.json(
            {
                "executorType": "edge",
                "deviceId": box.device_id,
                "householdId": entries[0].entry_id if entries else None,
            }
        )


class EdgeBootstrapView(_EdgeView):
    """A box never bootstraps here: the bootstrap reads `cloud_ep` only. One
    that reaches this path anyway gets a clear refusal, not a stray 200."""

    url = "/api/v1/edge-devices/bootstrap"
    name = "svitgrid:edge:bootstrap"

    async def post(self, request):
        state = await self._state(request)
        if isinstance(state, web.Response):
            return state
        return self.json({"error": "island_no_bootstrap"}, status_code=404)


class EdgeConfigView(_EdgeView):
    url = "/api/v1/edge-devices/config"
    name = "svitgrid:edge:config"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json(build_edge_config(box))


class EdgeSyncSettingsView(_EdgeView):
    """Feeds the LAN settings mirror (`/api/svitgrid/settings`).

    A body it cannot store answers 200 `{"ok": false, "error"}`: the
    firmware only checks for 200, and a 4xx would make it retry every cycle.
    """

    url = "/api/v1/edge-devices/sync-settings"
    name = "svitgrid:edge:sync-settings"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        snapshot, error = _snapshot_from_settings_body(box, await _json_body(request))
        if error is not None:
            _LOGGER.info("Edge box %s: settings not stored (%s)", box.device_id, error)
            return self.json({"ok": False, "error": error})
        await store_edge_settings_snapshot(request.app["hass"], snapshot)
        return self.json({"ok": True})


class EdgeFirmwareCheckView(_EdgeView):
    url = "/api/v3/executors/firmware-check"
    name = "svitgrid:edge:firmware-check"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({"offer": None, "reason": "island"})


class EdgeMqttTokenView(_EdgeView):
    url = "/api/v3/edge-devices/{device_id}/mqtt-token"
    name = "svitgrid:edge:mqtt-token"

    async def post(self, request, device_id: str):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({"error": "island_no_mqtt"}, status_code=404)


class EdgeCoredumpView(_EdgeView):
    """Not stored; the WARNING is the only crash trace an island owner sees."""

    url = "/api/v1/edge-devices/coredump"
    name = "svitgrid:edge:coredump"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        _LOGGER.warning(
            "Edge box %s crashed: resetReason=%s crashSummary=%s",
            box.device_id,
            body.get("resetReason"),
            body.get("crashSummary"),
        )
        return self.json({"stored": False, "reason": "island"})


class EdgePairAttemptsView(_EdgeView):
    url = "/api/v1/edge-devices/{device_id}/pair-attempts"
    name = "svitgrid:edge:pair-attempts"

    async def post(self, request, device_id: str):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({})


class EdgeUpdateInverterIpView(_EdgeView):
    """Records the inverter's new address on the box's entry. An inverter the
    box does not hold is ignored; the answer is 200 either way."""

    url = "/api/v1/edge-devices/update-inverter-ip"
    name = "svitgrid:edge:update-inverter-ip"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        inverter_id = body.get("inverterId")
        ip_address = body.get("ipAddress")
        if (
            isinstance(inverter_id, str)
            and isinstance(ip_address, str)
            and ip_address
            and inverter_id in box.inverters
        ):
            inverters = {
                **box.inverters,
                inverter_id: {**box.inverters[inverter_id], "ipAddress": ip_address},
            }
            keystore = self._keystore(request.app["hass"])
            await keystore.async_add_edge_box(replace(box, inverters=inverters))
        return self.json({})


class EdgeZigbeeSinkView(_EdgeView):
    """Zigbee is a non-goal on an island; the box must not see a 4xx storm."""

    url = "/api/v1/edge-devices/zigbee/device-joined"
    extra_urls = [
        "/api/v1/edge-devices/zigbee/state-update",
        "/api/v1/edge-devices/{device_id}/sensor-readings",
    ]
    name = "svitgrid:edge:zigbee"

    async def post(self, request, **_match):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({})


class EdgeTrustedKeysView(_EdgeView):
    url = "/api/v3/executors/trusted-keys"
    name = "svitgrid:edge:trusted-keys"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        state = await self._keystore(request.app["hass"]).load()
        return self.json(
            {
                "trustedKeys": [
                    {"keyId": key_id, "publicKeyHex": public_key_hex}
                    for key_id, public_key_hex in state.trusted_public_keys_hex.items()
                ]
            }
        )


EDGE_VIEWS = (
    EdgeHealthView,
    EdgeMeView,
    EdgeBootstrapView,
    EdgeConfigView,
    EdgeSyncSettingsView,
    EdgeFirmwareCheckView,
    EdgeMqttTokenView,
    EdgeCoredumpView,
    EdgePairAttemptsView,
    EdgeUpdateInverterIpView,
    EdgeZigbeeSinkView,
    EdgeTrustedKeysView,
)
