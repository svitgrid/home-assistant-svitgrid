"""Aiohttp wrapper for the Svitgrid cloud API. All errors mapped to typed
exceptions so callers can branch by meaning, not HTTP status code."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

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

    async def push_reading(
        self, api_key: str, reading: dict[str, Any]
    ) -> dict[str, Any] | None:
        """POST one reading. Returns the parsed response body on 2xx, else None.
        The caller (readings_publisher) uses `ingestIntervalMs` from the body
        to drive its adaptive sleep cadence."""
        url = f"{self._base}/api/v1/ingest/reading"
        async with self._session.post(
            url, headers={"x-api-key": api_key}, json=reading
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "push_reading failed: status=%s body=%s",
                    resp.status,
                    await _err(resp),
                )
                return None
            try:
                return await resp.json()
            except Exception:  # noqa: BLE001
                # 2xx with non-JSON body — log and treat as success with no
                # cadence hint (caller falls back to default).
                _LOGGER.debug("push_reading: 2xx with non-JSON body")
                return {}

    async def poll_commands(self, api_key: str) -> dict[str, Any]:
        url = f"{self._base}/api/v3/executors/commands"
        async with self._session.get(url, headers={"x-api-key": api_key}) as resp:
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
            url, headers={"x-api-key": api_key}, json={},
        ) as resp:
            if resp.status >= 400:
                raise SvitgridApiError(
                    f"mqtt-token failed: HTTP {resp.status}: {await _err(resp)}"
                )
            return await resp.json()

    async def ack_command(self, api_key: str, command_id: str, body: dict[str, Any]) -> None:
        url = f"{self._base}/api/v3/executors/commands/{command_id}/ack"
        async with self._session.post(url, headers={"x-api-key": api_key}, json=body) as resp:
            if resp.status >= 400:
                raise CommandAckFailed(f"HTTP {resp.status}: {await _err(resp)}")


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
