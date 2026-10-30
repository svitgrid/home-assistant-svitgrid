"""True island: the jobs the cloud command poller did, over the LAN.

True island is island mode on and the cloud copy off. The cloud then takes no
part in day-to-day operation (owner, 2026-10-15), so setup starts no command
poller, and whatever the poller used to carry reaches Home Assistant from the
app directly:

- `POST /api/svitgrid/island/cloud-copy` turns the cloud copy on or off.
- `POST /api/svitgrid/island/leave` leaves island mode.
- `POST /api/svitgrid/keys/revoke` untrusts a signing key, and removes the
  island key of the phone it belongs to.
- `GET /api/svitgrid/island/status` says which of these this integration
  serves. An integration without it answers 404, and the app falls back to the
  cloud path.
- `POST /api/svitgrid/edge/adopt` and `POST /api/svitgrid/edge/forget` add
  and remove an edge box, and `GET /api/svitgrid/edge/status` reports each one
  (edge island mode). Adopting the first box is what opens the edge-facing
  views (`edge_api`); an install that never calls these sees no change.
- `POST /api/svitgrid/edge/leave` queues the phone-signed
  `clear_island_endpoint` that sends a box back to its cloud endpoint, and
  `GET /api/svitgrid/edge/commands/{command_id}` reports how a queued command
  stands.
- `POST /api/svitgrid/edge/keys/add` queues the phone-signed
  `add_trusted_key` for every adopted box, and revoking a key queues
  `revoke_trusted_key` for each of them.

The three writes carry the phone's island key in `X-Island-Key` and are
signed by a trusted admin key over the canonical JSON of `signedData`, exactly
as `POST /api/svitgrid/events` is. `signedData` names its `action`, so a
request signed for one endpoint is refused by the others, and carries a
`nonce` and a `ts` (epoch milliseconds): a `ts` more than five minutes from
this clock is `stale_request`, and a nonce seen before is `replayed`.

The paths, fields, status codes and error codes are a contract shared with
the Svitgrid app (true-island-contract.md, 2026-10-15). Do not change one to
make a test pass.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState

from .api_client import async_integration_version
from .command_auth import verify_signed_command
from .const import (
    ADD_TRUSTED_KEY_COMMAND,
    CONF_LOCAL_ONLY,
    DOMAIN,
    REVOKE_TRUSTED_KEY_COMMAND,
)
from .edge_api import EDGE_BOX_LOCK, edge_refusals
from .edge_api import _entry_state as _edge_poll_state
from .entry_reload import update_entry_skipping_listener_reload
from .harvest.brand_commands import island_writes
from .island_auth import island_key_present_and_valid
from .keystore import EdgeBoxEntry, LastAdminKey
from .settings_sync import find_edge_settings_snapshot
from .signing import compute_key_id, public_key_from_hex

_LOGGER = logging.getLogger(__name__)

# How far `ts` may be from this clock, either way.
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
# How many nonces are remembered. In memory only: a restart forgets them, and
# the five-minute `ts` window bounds what a replay after a restart can reach.
NONCE_MEMORY = 500
_NONCES_KEY = "_island_lan_nonces"

# What GET /api/svitgrid/island/status advertises. The app picks the LAN path
# for a job only when its capability is listed here. `rules`: the island
# scheduler runs a rule (recurrence "always"), so the app shows the Rules tab
# of a true island. An older integration raised KeyError on every rule.
CAPABILITIES = (
    "cloud_copy_lan",
    "leave_lan",
    "revoke_lan",
    "pair_extra_phone",
    "rules",
    # GET /api/svitgrid/settings serves every brand's snapshot and tells
    # not_read_yet from nothing_to_configure; an app reads a 404 from a host
    # without it as "host too old".
    "settings_lan",
)

ACTION_SET_CLOUD_COPY = "set_cloud_copy"
ACTION_LEAVE_ISLAND = "leave_island"
ACTION_REVOKE_KEY = "revoke_key"
# `POST /api/svitgrid/trust-key`, served by http_views.SvitgridTrustKeyView.
ACTION_TRUST_KEY = "trust_key"
ACTION_EDGE_ADOPT = "edge_adopt"
ACTION_EDGE_FORGET = "edge_forget"
ACTION_EDGE_LEAVE = "edge_leave"
ACTION_EDGE_KEY_ADD = "edge_key_add"
# The box command `/edge/leave` queues (`edge_pair_code.json` `commands.clear`).
EDGE_LEAVE_COMMAND = "clear_island_endpoint"

# The per-inverter keys of an `edge_adopt` body kept on the box's entry: the
# box's own harvest config, as the cloud holds it. `build_edge_config` serves
# them back to the box and the reading pipeline reads its calibration here.
EDGE_ADOPT_INVERTER_KEYS = (
    "inverterId",
    "modelId",
    "protocolId",
    "transport",
    "ipAddress",
    "loggerSerial",
    "slaveId",
    "phases",
    "hasBattery",
    "pvStrings",
    "batteryCapacityWh",
    "batterySignInverted",
    "powerScaleOverrides",
)


def is_true_island(
    data: Mapping[str, Any], *, options: Mapping[str, Any] | None = None, has_island_key: bool
) -> bool:
    """True when the cloud takes no part in this entry's daily operation.

    A local-only entry always is: it has no account. A cloud-paired entry is
    when its cloud copy is off, as long as some phone holds an island key.
    With no island key nothing can reach the install over the LAN, and
    stopping the cloud command channel would strand it.
    """
    if data.get(CONF_LOCAL_ONLY):
        return True
    cloud_copy = data.get("cloud_ingest_enabled", (options or {}).get("cloud_ingest_enabled", True))
    return not cloud_copy and has_island_key


def _now_ms() -> int:
    """Epoch milliseconds. Seam for tests to pin the clock."""
    return int(time.time() * 1000)


def _json_error(status: int, error: str, **extra) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps({"error": error, **extra}),
        content_type="application/json",
    )


def _nonces(hass) -> OrderedDict[str, None]:
    data = hass.data.setdefault(DOMAIN, {})
    seen = data.get(_NONCES_KEY)
    if not isinstance(seen, OrderedDict):
        seen = data[_NONCES_KEY] = OrderedDict()
    return seen


def _remember_nonce(hass, nonce: str) -> bool:
    """Record `nonce`. False when it was seen before."""
    seen = _nonces(hass)
    if nonce in seen:
        return False
    seen[nonce] = None
    while len(seen) > NONCE_MEMORY:
        seen.popitem(last=False)
    return True


async def _island_key_ok(request) -> bool:
    hass = request.app["hass"]
    keystore = hass.data.get(DOMAIN, {}).get("keystore")
    island_keys = await keystore.async_get_island_keys() if keystore is not None else []
    return island_key_present_and_valid(request, island_keys)


async def signed_lan_request(request, action: str) -> tuple[dict | None, web.Response | None]:
    """Authenticate a signed management request.

    Returns `(signedData, None)` on success, or `(None, error response)`. The
    order is fixed: island key, body, signature, action, clock, nonce. The
    nonce is spent only by a request that passed everything before it, so a
    stranger cannot burn the app's nonces with unsigned requests.
    """
    hass = request.app["hass"]
    if not await _island_key_ok(request):
        return None, _json_error(401, "unauthorized")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None, _json_error(400, "bad_request")
    if not isinstance(body, dict):
        return None, _json_error(400, "bad_request")
    signing_key_id = body.get("signingKeyId")
    signed = body.get("signedData")
    signature = body.get("signature")
    if (
        not isinstance(signing_key_id, str)
        or not signing_key_id
        or not isinstance(signed, dict)
        or not isinstance(signature, str)
        or not signature
    ):
        return None, _json_error(400, "bad_request")

    keystore = hass.data.get(DOMAIN, {}).get("keystore")
    state = await keystore.load() if keystore is not None else None
    trusted = state.trusted_public_keys_hex if state is not None else {}
    if not verify_signed_command(trusted, signing_key_id, signed, signature):
        return None, _json_error(403, "signature_invalid")

    if signed.get("action") != action:
        return None, _json_error(400, "bad_request", detail=f"action must be {action}")
    nonce = signed.get("nonce")
    ts = signed.get("ts")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        return None, _json_error(400, "bad_request", detail="nonce must be a string")
    if isinstance(ts, bool) or not isinstance(ts, int | float):
        return None, _json_error(400, "bad_request", detail="ts must be epoch milliseconds")
    if abs(_now_ms() - ts) > MAX_CLOCK_SKEW_MS:
        return None, _json_error(403, "stale_request")
    if not _remember_nonce(hass, nonce):
        return None, _json_error(403, "replayed")
    return signed, None


def _entries(hass) -> list:
    return list(hass.config_entries.async_entries(DOMAIN))


def _cloud_copy_of(entry) -> bool:
    if entry.data.get(CONF_LOCAL_ONLY):
        return False
    return bool(
        entry.data.get("cloud_ingest_enabled", entry.options.get("cloud_ingest_enabled", True))
    )


def _set_cloud_copy(hass, entries: list, enabled: bool) -> int:
    """Write `cloud_ingest_enabled` on `entries` and reload each that changed.

    Never awaits the reload: it tears this integration down, and the caller's
    response must not wait on its own teardown. Returns how many changed.
    """
    changed = 0
    for entry in entries:
        if _cloud_copy_of(entry) == enabled:
            continue
        # The reload below is the only one: skip the update listener's.
        update_entry_skipping_listener_reload(
            hass, entry, {**entry.data, "cloud_ingest_enabled": enabled}
        )
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
        changed += 1
    return changed


class SvitgridIslandCloudCopyView(HomeAssistantView):
    """POST /api/svitgrid/island/cloud-copy — turn the cloud copy on or off.

    `signedData`: `{action: "set_cloud_copy", enabled: bool, nonce, ts}`.
    The app asks the API first, which answers 402 without a plan, and tells
    Home Assistant only after the API agreed. Turning it on reloads the entry
    and starts the cloud loops; turning it off reloads it into true island.
    A local-only entry has no cloud account: `enabled: true` is 409
    `no_cloud_identity`.
    """

    url = "/api/svitgrid/island/cloud-copy"
    name = "api:svitgrid:island:cloud-copy"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        signed, error = await signed_lan_request(request, ACTION_SET_CLOUD_COPY)
        if error is not None:
            return error
        enabled = signed.get("enabled")
        if not isinstance(enabled, bool):
            return _json_error(400, "bad_request", detail="enabled must be a boolean")
        hass = request.app["hass"]
        entries = _entries(hass)
        if not entries:
            return _json_error(404, "no_config_entry")
        cloud_entries = [e for e in entries if not e.data.get(CONF_LOCAL_ONLY)]
        if enabled and not cloud_entries:
            return _json_error(409, "no_cloud_identity")
        changed = _set_cloud_copy(hass, cloud_entries, enabled)
        _LOGGER.info("Cloud copy set to %s over the LAN (%d entry/ies reloaded)", enabled, changed)
        return web.json_response({"ok": True, "cloudCopy": enabled})


class SvitgridIslandLeaveView(HomeAssistantView):
    """POST /api/svitgrid/island/leave — leave island mode.

    `signedData`: `{action: "leave_island", nonce, ts}`. The same effect as
    the cloud's `disable_island` command: the cloud copy goes on, the entry
    reloads and the cloud loops start. Island keys stay, as they do for
    `disable_island`. A local-only entry has no cloud to return to: 409
    `no_cloud_identity`.
    """

    url = "/api/svitgrid/island/leave"
    name = "api:svitgrid:island:leave"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        _, error = await signed_lan_request(request, ACTION_LEAVE_ISLAND)
        if error is not None:
            return error
        hass = request.app["hass"]
        entries = _entries(hass)
        if not entries:
            return _json_error(404, "no_config_entry")
        cloud_entries = [e for e in entries if not e.data.get(CONF_LOCAL_ONLY)]
        if not cloud_entries:
            return _json_error(409, "no_cloud_identity")
        changed = _set_cloud_copy(hass, cloud_entries, True)
        _LOGGER.info("Left island mode over the LAN (%d entry/ies reloaded)", changed)
        return web.json_response({"ok": True})


class SvitgridKeysRevokeView(HomeAssistantView):
    """POST /api/svitgrid/keys/revoke — untrust a signing key.

    `signedData`: `{action: "revoke_key", keyId, nonce, ts}`. When the key
    belongs to a phone paired over the LAN, that phone's island key goes too,
    so it can neither sign nor read. 409 `last_admin_key` when it is the only
    trusted key: nothing could sign a command or a calendar write afterwards.
    An unknown key id is not an error.
    """

    url = "/api/svitgrid/keys/revoke"
    name = "api:svitgrid:keys:revoke"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        _, error = await revoke_signed_key(request)
        if error is not None:
            return error
        return web.json_response({"ok": True})


async def revoke_signed_key(
    request, *, path_key_id: str | None = None
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Run a signed `revoke_key` request. Returns `(outcome, None)` or
    `(None, error response)`; `outcome` is `async_revoke_signing_key`'s.

    Shared by `POST /api/svitgrid/keys/revoke` and its alias
    `DELETE /api/svitgrid/trust-key/{id}`. With `path_key_id`, the signed
    `keyId` must name the same key, so a signature for one key cannot be
    sent to another key's path.
    """
    signed, error = await signed_lan_request(request, ACTION_REVOKE_KEY)
    if error is not None:
        return None, error
    key_id = signed.get("keyId")
    if not isinstance(key_id, str) or not key_id:
        return None, _json_error(400, "bad_request", detail="keyId must be a string")
    if path_key_id is not None and key_id != path_key_id:
        return None, _json_error(400, "bad_request", detail="keyId must match the path")
    hass = request.app["hass"]
    keystore = hass.data.get(DOMAIN, {}).get("keystore")
    if keystore is None:
        return None, _json_error(503, "unavailable")
    try:
        outcome = await keystore.async_revoke_signing_key(key_id)
    except LastAdminKey:
        return None, _json_error(409, "last_admin_key")
    _LOGGER.info(
        "Revoked signing key %s over the LAN (untrusted=%s, island device=%s)",
        key_id,
        outcome["untrusted"],
        outcome["deviceId"],
    )
    # A revoked phone's Telegram chat goes with its island key.
    from .telegram.views import unlink_device

    await unlink_device(hass, outcome["deviceId"])
    # The box takes a revocation without a signature (`command_auth.c`). A box
    # that never held the key acks it as a no-op.
    await _enqueue_for_every_box(
        hass, REVOKE_TRUSTED_KEY_COMMAND, {"keyIdToRevoke": key_id}, signing=None, source="revoke"
    )
    return outcome, None


async def _enqueue_for_every_box(
    hass, command: str, payload: dict, *, signing: dict | None, source: str
) -> dict[str, str] | None:
    """Queue `command` for every adopted edge box. Returns `{deviceId:
    commandId}`, empty with no box adopted, or None when boxes are adopted
    and no queue is running."""
    keystore = hass.data.get(DOMAIN, {}).get("keystore")
    state = await keystore.load() if keystore is not None else None
    device_ids = list(state.edge_boxes) if state is not None else []
    if not device_ids:
        return {}
    # The queue the box drains on its poll (`EdgeCommandsPollView`).
    queue = (_edge_poll_state(hass) or {}).get("edge_queue")
    if queue is None:
        _LOGGER.warning(
            "No edge command queue: %s not sent to %d box(es)", command, len(device_ids)
        )
        return None
    return {
        device_id: await queue.async_enqueue(
            device_id, command, payload, signing=signing, inverter_id=None, source=source
        )
        for device_id in device_ids
    }


class SvitgridIslandStatusView(HomeAssistantView):
    """GET /api/svitgrid/island/status — what this station is, and what it serves.

    Island key only: no HA session, no signature. `capabilities` tells the app
    which jobs go over the LAN; an integration older than this view answers
    404, and the app uses the cloud path.
    """

    url = "/api/svitgrid/island/status"
    name = "api:svitgrid:island:status"
    requires_auth = False

    async def get(self, request) -> web.Response:  # noqa: D102
        if not await _island_key_ok(request):
            return _json_error(401, "unauthorized")
        hass = request.app["hass"]
        entries = _entries(hass)
        if not entries:
            return _json_error(404, "no_config_entry")
        entry = entries[0]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        has_island_key = bool(await keystore.async_get_island_keys()) if keystore else False
        body = {
            "cloudCopy": _cloud_copy_of(entry),
            "localOnly": bool(entry.data.get(CONF_LOCAL_ONLY)),
            "trueIsland": is_true_island(
                entry.data, options=entry.options, has_island_key=has_island_key
            ),
            "version": await async_integration_version(hass),
            "capabilities": list(CAPABILITIES),
        }
        state = await keystore.load() if keystore is not None else None
        boxes = list(state.edge_boxes.values()) if state is not None else []
        if boxes:
            # Only once a box is adopted: an install without one answers as before.
            body["edgeBoxes"] = [
                {k: status[k] for k in _ISLAND_STATUS_BOX_KEYS}
                for status in [await _box_status(hass, box) for box in boxes]
            ]
            body["pendingBoxKeys"] = await keystore.async_list_pending_box_keys()
        inverters = _inverter_adverts(hass, boxes)
        if inverters:
            body["inverters"] = inverters
        return web.json_response(body)


def _inverter_adverts(hass, boxes: list[EdgeBoxEntry]) -> dict[str, dict]:
    """What the island writes for each inverter, keyed by inverter id.

    An edge box's inverter lists `writableActions` and `modes` from its brand
    (`island_writes`). One Home Assistant harvests names only its host: with
    no `writableActions` the app keeps the rule it has always applied, so an
    install without a box sees no change.
    """
    adverts: dict[str, dict] = {}
    for entry in _entries(hass):
        for inverter in entry.data.get("inverters") or []:
            inverter_id = inverter.get("inverter_id") if isinstance(inverter, dict) else None
            if isinstance(inverter_id, str) and inverter_id:
                adverts[inverter_id] = {"host": "ha"}
    for box in boxes:
        for inverter_id in box.inverter_ids:
            adopted = box.inverters.get(inverter_id) or {}
            adverts[inverter_id] = {"host": "edge", **island_writes(adopted)}
    return adverts


# What `/island/status` says of each edge box; `/edge/status` says more.
_ISLAND_STATUS_BOX_KEYS = ("deviceId", "lastContactAt", "version", "pendingCommands", "inverterIds")


def _wire_time(iso: str | None) -> str | None:
    """A stored ISO time as `YYYY-MM-DDTHH:MM:SS.fffZ`, or None."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _device_id(signed: dict) -> str | None:
    device_id = signed.get("deviceId")
    return device_id if isinstance(device_id, str) and device_id else None


def _adopt_inverters(raw) -> list[dict] | None:
    """The adopt body's inverters, each cut to `EDGE_ADOPT_INVERTER_KEYS`, or
    None when the list is malformed: not a list, an entry without a string
    `inverterId` or `modelId`, or one inverter twice."""
    if not isinstance(raw, list):
        return None
    inverters: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        inverter_id = item.get("inverterId")
        model_id = item.get("modelId")
        if not isinstance(inverter_id, str) or not inverter_id:
            return None
        if not isinstance(model_id, str) or not model_id:
            return None
        if any(i["inverterId"] == inverter_id for i in inverters):
            return None
        inverters.append({k: item[k] for k in EDGE_ADOPT_INVERTER_KEYS if k in item})
    return inverters


def _edge_entry_states(hass) -> list[dict]:
    """Every config entry's state that holds an edge command queue."""
    return [
        state
        for state in (hass.data.get(DOMAIN) or {}).values()
        if isinstance(state, dict) and state.get("edge_queue") is not None
    ]


def _schedule_edge_reload(hass) -> None:
    """Reload every set-up entry once the current answer is out.

    Setup reads the edge boxes: it builds the edge queue and an
    `EdgeBoxExecutor` per adopted inverter, and stops harvesting what a box
    holds. The keystore is shared, so each loaded entry is reloaded. Deferred
    past the handler: the reload starts eagerly, and the adopt answer carries
    the pairing code, which must reach the app first.
    """
    domain_data = hass.data.get(DOMAIN) or {}
    for entry in _entries(hass):
        state = domain_data.get(entry.entry_id)
        if entry.state is not ConfigEntryState.LOADED or not isinstance(state, dict):
            continue
        if state.get("store") is None:
            continue
        hass.loop.call_soon(hass.config_entries.async_schedule_reload, entry.entry_id)


def _first_store(hass):
    for state in (hass.data.get(DOMAIN) or {}).values():
        if isinstance(state, dict) and state.get("store") is not None:
            return state["store"]
    return None


class SvitgridEdgeAdoptView(HomeAssistantView):
    """POST /api/svitgrid/edge/adopt — adopt one edge box and its inverters.

    `signedData`: `{action: "edge_adopt", deviceId, inverters: [{inverterId,
    modelId, ...}], pollIntervalMs?, nonce, ts}`. The box is stored unbound,
    with a fresh pairing code, which this answer carries in clear and nothing
    else ever does: `{ok, pairCode, expiresAt}`. Adopting the same box again
    replaces its inverters, drops its binding and issues a new code, which is
    the app's retry after `pair_code_expired`. An inverter another box holds
    is 409 `inverter_held_by_other_box`.
    """

    url = "/api/svitgrid/edge/adopt"
    name = "api:svitgrid:edge:adopt"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        signed, error = await signed_lan_request(request, ACTION_EDGE_ADOPT)
        if error is not None:
            return error
        device_id = _device_id(signed)
        if device_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        inverters = _adopt_inverters(signed.get("inverters"))
        if inverters is None:
            return _json_error(
                400, "bad_request", detail="inverters must list inverterId and modelId once each"
            )
        hass = request.app["hass"]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        if keystore is None:
            return _json_error(503, "unavailable")
        inverter_ids = [i["inverterId"] for i in inverters]
        async with EDGE_BOX_LOCK:
            state = await keystore.load()
            if state is None:
                return _json_error(503, "unavailable")
            for other in state.edge_boxes.values():
                if other.device_id == device_id:
                    continue
                held = set(other.inverter_ids) | set(other.inverters)
                if held.intersection(inverter_ids):
                    return _json_error(409, "inverter_held_by_other_box")
            previous = state.edge_boxes.get(device_id)
            await keystore.async_add_edge_box(
                EdgeBoxEntry(
                    device_id=device_id,
                    api_key=None,
                    inverter_ids=inverter_ids,
                    inverters={i["inverterId"]: i for i in inverters},
                    added_at=previous.added_at
                    if previous is not None and previous.added_at
                    else datetime.now(UTC).isoformat(),
                    pair_code_hash=None,
                    pair_code_expires_at=None,
                )
            )
            code = await keystore.async_issue_pair_code(device_id)
            state = await keystore.load()
            expires_at = state.edge_boxes[device_id].pair_code_expires_at
        edge_refusals(hass).pop(device_id, None)
        _LOGGER.info(
            "Edge box %s adopted over the LAN with %d inverter(s)", device_id, len(inverters)
        )
        _schedule_edge_reload(hass)
        return web.json_response(
            {"ok": True, "pairCode": code, "expiresAt": _wire_time(expires_at)}
        )


class SvitgridEdgeForgetView(HomeAssistantView):
    """POST /api/svitgrid/edge/forget — forget one edge box.

    `signedData`: `{action: "edge_forget", deviceId, nonce, ts}`. Removes the
    box and every command queued for it; other boxes stay. Idempotent: a box
    this integration does not hold is `{ok: true}` too. With the last box gone
    the edge-facing views answer 404 again.
    """

    url = "/api/svitgrid/edge/forget"
    name = "api:svitgrid:edge:forget"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        signed, error = await signed_lan_request(request, ACTION_EDGE_FORGET)
        if error is not None:
            return error
        device_id = _device_id(signed)
        if device_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        hass = request.app["hass"]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        if keystore is None:
            return _json_error(503, "unavailable")
        async with EDGE_BOX_LOCK:
            removed = await keystore.async_remove_edge_box(device_id)
        for state in _edge_entry_states(hass):
            await state["edge_queue"].async_forget_device(device_id)
        edge_refusals(hass).pop(device_id, None)
        if removed:
            _LOGGER.info("Edge box %s forgotten over the LAN", device_id)
            _schedule_edge_reload(hass)
        return web.json_response({"ok": True})


class SvitgridEdgeStatusView(HomeAssistantView):
    """GET /api/svitgrid/edge/status — every adopted edge box.

    Island key only. `{boxes: [{deviceId, bound, codeExpiresAt, lastContactAt,
    version, pendingCommands, inverterIds, lastSettingsReadAt, lastRefusal}],
    pendingBoxKeys: [{keyId, label, publicKeyHex}]}`. `lastRefusal` is the
    last `pair_code_*` answer the box got, `{error, at}`, so the app can tell
    an expired code from a box that never called. `pendingBoxKeys`, listed
    once a box is adopted, are the keys trusted here that a box does not hold
    yet (`/edge/keys/add`).
    """

    url = "/api/svitgrid/edge/status"
    name = "api:svitgrid:edge:status"
    requires_auth = False

    async def get(self, request) -> web.Response:  # noqa: D102
        if not await _island_key_ok(request):
            return _json_error(401, "unauthorized")
        hass = request.app["hass"]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        state = await keystore.load() if keystore is not None else None
        boxes = list(state.edge_boxes.values()) if state is not None else []
        body: dict[str, Any] = {"boxes": [await _box_status(hass, box) for box in boxes]}
        if boxes:
            body["pendingBoxKeys"] = await keystore.async_list_pending_box_keys()
        return web.json_response(body)


async def _box_status(hass, box: EdgeBoxEntry) -> dict[str, Any]:
    store = _first_store(hass)
    contact = (await store.get_edge_box_state(box.device_id) if store is not None else None) or {}
    pending = 0
    for state in _edge_entry_states(hass):
        pending += await state["edge_queue"].async_pending_count(box.device_id)
    read_ats = []
    for inverter_id in box.inverter_ids:
        snapshot, _ = await find_edge_settings_snapshot(hass, inverter_id)
        if snapshot is not None and isinstance(snapshot.get("read_at"), str):
            read_ats.append(snapshot["read_at"])
    refusal = edge_refusals(hass).get(box.device_id)
    bound = box.api_key is not None
    return {
        "deviceId": box.device_id,
        "bound": bound,
        "codeExpiresAt": None if bound else _wire_time(box.pair_code_expires_at),
        "lastContactAt": contact.get("lastContactAt"),
        "version": contact.get("version"),
        "pendingCommands": pending,
        "inverterIds": list(box.inverter_ids),
        "lastSettingsReadAt": max(read_ats) if read_ats else None,
        "lastRefusal": dict(refusal) if refusal is not None else None,
    }


def _box_command(raw) -> dict | None:
    """The leave body's `boxCommand` when it carries a phone signature for
    the box: `signedEventData` (an object), `signature` and `signingKeyId`
    (non-empty strings). None otherwise."""
    if not isinstance(raw, dict):
        return None
    signed = raw.get("signedEventData")
    signature = raw.get("signature")
    key_id = raw.get("signingKeyId")
    if not isinstance(signed, dict):
        return None
    if not isinstance(signature, str) or not signature:
        return None
    if not isinstance(key_id, str) or not key_id:
        return None
    return {"signedEventData": signed, "signature": signature, "signingKeyId": key_id}


class SvitgridEdgeLeaveView(HomeAssistantView):
    """POST /api/svitgrid/edge/leave — send one edge box back to the cloud.

    `signedData`: `{action: "edge_leave", deviceId, boxCommand, nonce, ts}`.
    `boxCommand` is `{signedEventData: {command: "clear_island_endpoint",
    payload: {}, validUntil}, signature, signingKeyId}`, signed by the phone
    for the box: the integration holds no key the box trusts, so a leave
    without it is 400 `box_signature_required`. It is queued for the box with
    an empty payload and the signature untouched; no URL travels, the box
    falls back to its own cloud endpoint. Answers `{commandId}`, which
    `/edge/commands/{command_id}` reports on. A box this integration does not
    hold is 404 `unknown_box`.
    """

    url = "/api/svitgrid/edge/leave"
    name = "api:svitgrid:edge:leave"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        signed, error = await signed_lan_request(request, ACTION_EDGE_LEAVE)
        if error is not None:
            return error
        device_id = _device_id(signed)
        if device_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        box_command = _box_command(signed.get("boxCommand"))
        if box_command is None:
            return _json_error(400, "box_signature_required")
        event = box_command["signedEventData"]
        if event.get("command") != EDGE_LEAVE_COMMAND or event.get("payload", {}) != {}:
            return _json_error(
                400,
                "bad_request",
                detail="boxCommand must be clear_island_endpoint with an empty payload",
            )
        hass = request.app["hass"]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        state = await keystore.load() if keystore is not None else None
        if state is None:
            return _json_error(503, "unavailable")
        if device_id not in state.edge_boxes:
            return _json_error(404, "unknown_box")
        # The queue the box drains on its poll (`EdgeCommandsPollView`).
        queue = (_edge_poll_state(hass) or {}).get("edge_queue")
        if queue is None:
            return _json_error(503, "unavailable")
        command_id = await queue.async_enqueue(
            device_id,
            EDGE_LEAVE_COMMAND,
            {},
            signing=box_command,
            inverter_id=None,
            source="leave",
        )
        _LOGGER.info("Edge box %s sent back to the cloud over the LAN (%s)", device_id, command_id)
        return web.json_response({"commandId": command_id})


def _box_key_command(raw) -> dict | None:
    """The key-add body's `boxCommand` when it carries a phone signature for
    the box: `payload` exactly `{publicKeyHex}`, `signature` and `signerKeyId`
    (non-empty strings). None otherwise."""
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload")
    signature = raw.get("signature")
    signer = raw.get("signerKeyId")
    if not isinstance(payload, dict) or not isinstance(payload.get("publicKeyHex"), str):
        return None
    if not isinstance(signature, str) or not signature:
        return None
    if not isinstance(signer, str) or not signer:
        return None
    return {"payload": payload, "signature": signature, "signerKeyId": signer}


class SvitgridEdgeKeyAddView(HomeAssistantView):
    """POST /api/svitgrid/edge/keys/add — pass a phone's key to every box.

    `signedData`: `{action: "edge_key_add", keyId, boxCommand, nonce, ts}`.
    `boxCommand` is `{payload: {publicKeyHex}, signature, signerKeyId}`: the
    phone's signature over the canonical `payload`, by a key the box already
    trusts, which the box checks itself (`handle_key_management_command`).
    The integration holds no key the box trusts, so a request without it is
    400 `box_signature_required`. `keyId` must be the fingerprint of
    `publicKeyHex`, or 400 `key_id_mismatch`. The `add_trusted_key` is queued
    for every adopted box with `signature` and `signerKeyId` beside the
    payload, where the box reads them; a box's success ack takes the key off
    `pendingBoxKeys` for that box. Answers `{ok, commandIds: {deviceId:
    commandId}}`. With no box adopted, 404 `no_edge_boxes`.
    """

    url = "/api/svitgrid/edge/keys/add"
    name = "api:svitgrid:edge:keys:add"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        signed, error = await signed_lan_request(request, ACTION_EDGE_KEY_ADD)
        if error is not None:
            return error
        box_command = _box_key_command(signed.get("boxCommand"))
        if box_command is None:
            return _json_error(400, "box_signature_required")
        payload = box_command["payload"]
        if set(payload) != {"publicKeyHex"}:
            return _json_error(
                400, "bad_request", detail="boxCommand payload must be exactly publicKeyHex"
            )
        try:
            public_key_from_hex(payload["publicKeyHex"])
        except Exception:  # noqa: BLE001
            return _json_error(400, "bad_public_key")
        key_id = signed.get("keyId")
        if not isinstance(key_id, str) or key_id != compute_key_id(payload["publicKeyHex"]):
            return _json_error(400, "key_id_mismatch")
        hass = request.app["hass"]
        command_ids = await _enqueue_for_every_box(
            hass,
            ADD_TRUSTED_KEY_COMMAND,
            payload,
            signing={k: box_command[k] for k in ("signature", "signerKeyId")},
            source="key_add",
        )
        if command_ids is None:
            return _json_error(503, "unavailable")
        if not command_ids:
            return _json_error(404, "no_edge_boxes")
        _LOGGER.info("Key %s queued for %d edge box(es) over the LAN", key_id, len(command_ids))
        return web.json_response({"ok": True, "commandIds": command_ids})


class SvitgridEdgeCommandView(HomeAssistantView):
    """GET /api/svitgrid/edge/commands/{command_id} — one queued command.

    Island key only. `{id, state, result, reason, ackedAt}`, where `state` is
    `pending`, `delivered`, `success`, `failed` or `expired`. An id no queue
    holds is 404 `unknown_command`.
    """

    url = "/api/svitgrid/edge/commands/{command_id}"
    name = "api:svitgrid:edge:command"
    requires_auth = False

    async def get(self, request, command_id: str) -> web.Response:  # noqa: D102
        if not await _island_key_ok(request):
            return _json_error(401, "unauthorized")
        for state in _edge_entry_states(request.app["hass"]):
            row = await state["edge_queue"].async_get(command_id)
            if row is not None:
                return web.json_response(
                    {
                        "id": row["id"],
                        "state": row["state"],
                        "result": row["result"],
                        "reason": row["reason"],
                        "ackedAt": row["acked_at"],
                    }
                )
        return _json_error(404, "unknown_command")


ISLAND_LAN_VIEWS = (
    SvitgridIslandCloudCopyView,
    SvitgridIslandLeaveView,
    SvitgridKeysRevokeView,
    SvitgridIslandStatusView,
    SvitgridEdgeAdoptView,
    SvitgridEdgeForgetView,
    SvitgridEdgeStatusView,
    SvitgridEdgeLeaveView,
    SvitgridEdgeKeyAddView,
    SvitgridEdgeCommandView,
)
