"""Aiohttp wrapper for the Svitgrid cloud API. All errors mapped to typed
exceptions so callers can branch by meaning, not HTTP status code."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiohttp


@lru_cache(maxsize=1)
def _integration_version() -> str | None:
    """The installed integration version (manifest.json next to this module),
    reported as `haVersion` on batch ingests so the cloud can census HA
    installs. Fail-open: any read/parse problem returns None and the field is
    simply omitted — version reporting must never break ingestion."""
    try:
        manifest = json.loads((Path(__file__).parent / "manifest.json").read_text())
        v = manifest.get("version")
        return str(v) if v else None
    except Exception:  # noqa: BLE001 — census is best-effort
        return None


_LOGGER = logging.getLogger(__name__)


class SvitgridApiError(Exception):
    """Base class for all API client errors."""


class BootstrapFailed(SvitgridApiError):
    """Generic bootstrap failure (5xx or unexpected response)."""


class DeviceNotFound(SvitgridApiError):
    """Device was not pre-associated, or the mobile app didn't open a bootstrap window."""


class PublicKeyMismatch(SvitgridApiError):
    """signingKeyId is already registered with a different publicKeyHex."""


class BootstrapWindowExpired(SvitgridApiError):
    """The 10-minute bootstrap window has closed; re-associate in the mobile app."""


class RateLimited(SvitgridApiError):
    """Too many bootstrap attempts for this deviceId."""


class CommandAckFailed(SvitgridApiError):
    """POST /commands/:id/ack returned non-2xx."""


class DeviceEvicted(SvitgridApiError):
    """Server returned 410 Gone on an authenticated poll — the device key was
    revoked (owning household deleted). Authoritative: callers must STOP
    polling, not retry."""


class ReadingRejected(SvitgridApiError):
    """`push_reading` got a 4xx (client error) on `/ingest/reading`.

    The payload is structurally wrong, incomplete, or unauthorized — retrying
    at the normal cadence just hammers the server with requests it will keep
    rejecting (each one still costs a Cloud Run request + auth read). Callers
    should back off HARD until the underlying config/firmware changes; the
    readings_publisher parks at its ceiling interval. Distinct from a 5xx /
    network blip, which is transient and returns None (normal-cadence retry)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status


class DeviceStopped(SvitgridApiError):
    """Server responded 200 with body `{stopped: true, stoppedReason: "..."}` on
    an authenticated request. An operator flipped `disabled: true` on this
    integration's edge-device doc — typically to evict a runaway/zombie poller.
    Callers should treat this as a graceful, soft eviction: stop polling/pushing
    and exit the loop. Recovery: operator clears the flag and the user reloads
    the integration."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SvitgridApiClient:
    """Thin wrapper around a shared aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession, api_base: str) -> None:
        self._session = session
        self._base = api_base.rstrip("/")

    async def bootstrap(
        self, device_id: str, public_key_hex: str, signing_key_id: str
    ) -> dict[str, Any]:
        url = f"{self._base}/api/v1/edge-devices/bootstrap"
        body = {
            "deviceId": device_id,
            "publicKeyHex": public_key_hex,
            "signingKeyId": signing_key_id,
        }
        async with self._session.post(url, json=body) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                raise DeviceNotFound(await _err(resp))
            if resp.status == 409:
                raise PublicKeyMismatch(await _err(resp))
            if resp.status == 410:
                raise BootstrapWindowExpired(await _err(resp))
            if resp.status == 429:
                raise RateLimited(await _err(resp))
            raise BootstrapFailed(f"HTTP {resp.status}: {await _err(resp)}")

    async def push_reading(self, api_key: str, reading: dict[str, Any]) -> dict[str, Any] | None:
        """POST one reading. Returns the parsed response body on 2xx, else None.
        The caller (readings_publisher) uses `ingestIntervalMs` from the body
        to drive its adaptive sleep cadence."""
        url = f"{self._base}/api/v1/ingest/reading"
        async with self._session.post(url, headers={"x-api-key": api_key}, json=reading) as resp:
            if resp.status >= 500:
                # Transient (server outage / overload) — caller retries at the
                # normal cadence.
                _LOGGER.warning(
                    "push_reading failed (transient): status=%s body=%s",
                    resp.status,
                    await _err(resp),
                )
                return None
            if resp.status >= 400:
                # Hard client error (validation, auth, gone). Re-POSTing the
                # same payload won't help — raise so the caller backs off hard
                # instead of hammering once per cadence tick.
                body = await _err(resp)
                _LOGGER.warning("push_reading rejected: status=%s body=%s", resp.status, body)
                raise ReadingRejected(resp.status, body)
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001
                # 2xx with non-JSON body — log and treat as success with no
                # cadence hint (caller falls back to default).
                _LOGGER.debug("push_reading: 2xx with non-JSON body")
                return {}
            if isinstance(body, dict) and body.get("stopped") is True:
                raise DeviceStopped(str(body.get("stoppedReason") or "unknown"))
            return body

    async def poll_commands(
        self, api_key: str, integration_version: str | None = None
    ) -> dict[str, Any]:
        url = f"{self._base}/api/v3/executors/commands"
        headers = {"x-api-key": api_key}
        if integration_version:
            headers["x-integration-version"] = integration_version
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 410:
                raise DeviceEvicted(await _err(resp))
            if resp.status >= 400:
                _LOGGER.warning(
                    "poll_commands failed: status=%s body=%s", resp.status, await _err(resp)
                )
                return {"commands": [], "serverTime": None}
            data = await resp.json()
            # Normalize: server sends `id` as the doc ID; downstream code
            # (command_poller.process_command) reads `commandId`. Aliasing
            # here keeps the wire format change contained at the boundary.
            for cmd in data.get("commands", []):
                if "id" in cmd and "commandId" not in cmd:
                    cmd["commandId"] = cmd["id"]
            if data.get("stopped") is True:
                raise DeviceStopped(str(data.get("stoppedReason") or "unknown"))
            return data

    async def get_mqtt_token(self, api_key: str) -> dict[str, Any]:
        """Mint a short-lived JWT for the MQTT wake-bell broker. Returns
        `{token, expiresAt, broker: {host, port, topic}}`.

        The :id path parameter is informational — identity comes from the
        x-api-key header. The bridge maps the api-key to the Firestore
        edgeDevices doc id, which is what the wake topic keys on.

        Used by mqtt_wake.run_loop; called on initial connect and again
        on every reconnect (JWTs are short-lived and re-minted)."""
        # Path param value doesn't matter — server identifies the device
        # from x-api-key. Use a placeholder so the route matches.
        url = f"{self._base}/api/v3/edge-devices/_/mqtt-token"
        async with self._session.post(
            url,
            headers={"x-api-key": api_key},
            json={},
        ) as resp:
            if resp.status >= 400:
                raise SvitgridApiError(f"mqtt-token failed: HTTP {resp.status}: {await _err(resp)}")
            return await resp.json()

    async def ack_command(self, api_key: str, command_id: str, body: dict[str, Any]) -> None:
        url = f"{self._base}/api/v3/executors/commands/{command_id}/ack"
        async with self._session.post(url, headers={"x-api-key": api_key}, json=body) as resp:
            if resp.status >= 400:
                raise CommandAckFailed(f"HTTP {resp.status}: {await _err(resp)}")

    async def push_readings_batch(
        self, api_key: str, readings: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """POST a batch of readings. Returns parsed body on 2xx, None on 5xx
        (transient), raises ReadingRejected on 4xx (permanent)."""
        url = f"{self._base}/api/v1/ingest/readings"
        body: dict[str, Any] = {"readings": readings}
        # v0.15.3: report the integration version so the cloud can census HA
        # installs (server stamps edgeDevices.version only when it changes).
        ha_version = _integration_version()
        if ha_version:
            body["haVersion"] = ha_version
        async with self._session.post(url, headers={"x-api-key": api_key}, json=body) as resp:
            if resp.status >= 500:
                _LOGGER.warning(
                    "push_readings_batch failed (transient): status=%s body=%s",
                    resp.status,
                    await _err(resp),
                )
                return None
            if resp.status == 410:
                raise DeviceEvicted(await _err(resp))
            if resp.status >= 400:
                body = await _err(resp)
                _LOGGER.warning(
                    "push_readings_batch rejected: status=%s body=%s", resp.status, body
                )
                raise ReadingRejected(resp.status, body)
            try:
                return await resp.json()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("push_readings_batch: 2xx with non-JSON body")
                return {}

    async def get_preset(self, preset_id: str) -> dict | None:
        """GET a live preset (entityMap + version) from the public presets endpoint.

        Returns the parsed JSON dict on 200, or None on any non-200 status.
        The endpoint is public — no api-key header is required."""
        url = f"{self._base}/api/v1/ha-presets/{preset_id}"
        async with self._session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            _LOGGER.debug("get_preset(%s) returned status=%s", preset_id, resp.status)
            return None

    async def get_register_spec(self, model_id: str) -> dict | None:
        """GET a register spec (registers + version) from the public register-specs endpoint.

        Returns the parsed JSON dict on 200, or None on any non-200 status.
        The endpoint is public — no api-key header is required."""
        url = f"{self._base}/api/v1/register-specs/{model_id}"
        async with self._session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            _LOGGER.debug("get_register_spec(%s) returned status=%s", model_id, resp.status)
            return None

    async def add_inverter(
        self,
        *,
        api_key: str,
        preset_id: str | None = None,
        inverter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/ha/inverters — register an additional inverter under
        this add-on's edge device. Exactly one of preset_id / inverter.
        Returns {inverterId, entityMap, brand, model, phases, hasBattery,
        pvStrings, commands, presetId}."""
        if bool(preset_id) == bool(inverter):
            raise SvitgridApiError("add_inverter requires exactly one of preset_id or inverter")
        body: dict[str, Any] = {"presetId": preset_id} if preset_id else {"inverter": inverter}
        url = f"{self._base}/api/v1/ha/inverters"
        async with self._session.post(url, headers={"x-api-key": api_key}, json=body) as resp:
            if resp.status >= 400:
                raise SvitgridApiError(
                    f"add_inverter failed: HTTP {resp.status}: {await _err(resp)}"
                )
            return await resp.json()


async def _err(resp: aiohttp.ClientResponse) -> str:
    """Render a server error for logs. Includes the full JSON body so
    Zod's `details[]` (which names the offending fields) is visible — the
    previous `body.get("error", body)` form hid that array and forced
    every validation regression into a print-statement debug session."""
    try:
        body = await resp.json()
        return str(body)
    except Exception:  # noqa: BLE001
        return "<non-JSON body>"
