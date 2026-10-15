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
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .api_client import _integration_version
from .command_auth import verify_signed_command
from .const import CONF_LOCAL_ONLY, DOMAIN
from .entry_reload import update_entry_skipping_listener_reload
from .island_auth import island_key_present_and_valid
from .keystore import LastAdminKey

_LOGGER = logging.getLogger(__name__)

# How far `ts` may be from this clock, either way.
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
# How many nonces are remembered. In memory only: a restart forgets them, and
# the five-minute `ts` window bounds what a replay after a restart can reach.
NONCE_MEMORY = 500
_NONCES_KEY = "_island_lan_nonces"

# What GET /api/svitgrid/island/status advertises. The app picks the LAN path
# for a job only when its capability is listed here.
CAPABILITIES = ("cloud_copy_lan", "leave_lan", "revoke_lan", "pair_extra_phone")

ACTION_SET_CLOUD_COPY = "set_cloud_copy"
ACTION_LEAVE_ISLAND = "leave_island"
ACTION_REVOKE_KEY = "revoke_key"


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


async def _signed_request(request, action: str) -> tuple[dict | None, web.Response | None]:
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
        signed, error = await _signed_request(request, ACTION_SET_CLOUD_COPY)
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
        _, error = await _signed_request(request, ACTION_LEAVE_ISLAND)
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
        signed, error = await _signed_request(request, ACTION_REVOKE_KEY)
        if error is not None:
            return error
        key_id = signed.get("keyId")
        if not isinstance(key_id, str) or not key_id:
            return _json_error(400, "bad_request", detail="keyId must be a string")
        hass = request.app["hass"]
        keystore = hass.data.get(DOMAIN, {}).get("keystore")
        if keystore is None:
            return _json_error(503, "unavailable")
        try:
            outcome = await keystore.async_revoke_signing_key(key_id)
        except LastAdminKey:
            return _json_error(409, "last_admin_key")
        _LOGGER.info(
            "Revoked signing key %s over the LAN (untrusted=%s, island device=%s)",
            key_id,
            outcome["untrusted"],
            outcome["deviceId"],
        )
        return web.json_response({"ok": True})


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
        return web.json_response(
            {
                "cloudCopy": _cloud_copy_of(entry),
                "localOnly": bool(entry.data.get(CONF_LOCAL_ONLY)),
                "trueIsland": is_true_island(
                    entry.data, options=entry.options, has_island_key=has_island_key
                ),
                "version": _integration_version(),
                "capabilities": list(CAPABILITIES),
            }
        )


ISLAND_LAN_VIEWS = (
    SvitgridIslandCloudCopyView,
    SvitgridIslandLeaveView,
    SvitgridKeysRevokeView,
    SvitgridIslandStatusView,
)
