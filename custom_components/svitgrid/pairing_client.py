"""HTTP client for the /ha-pairing/* cloud endpoints."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


class PairingError(Exception):
    """Base class for pairing client errors."""


class PairingNotFound(PairingError):
    """404 — secret/code unknown to the server."""


class PairingExpired(PairingError):
    """410 — pairing TTL elapsed."""


class PairingConflict(PairingError):
    """409 — pairing in wrong state (already claimed / not yet claimed)."""


@dataclass
class PairingPending:
    """Pairing exists, waiting for mobile to claim."""


@dataclass
class PairingClaimed:
    """Pairing has been claimed by a mobile user."""
    household_id: str
    preset_id: str | None


class PairingClient:
    """Wraps the 4 cloud pairing endpoints. One instance per integration setup."""

    def __init__(self, session: aiohttp.ClientSession, *, api_base: str) -> None:
        self._session = session
        self._base = api_base.rstrip("/")

    async def start(
        self, *, public_key_hex: str, signing_key_id: str
    ) -> dict[str, Any]:
        """POST /api/v1/ha-pairing/start — initiate. Returns {secret, code, expiresIn}."""
        url = f"{self._base}/api/v1/ha-pairing/start"
        async with self._session.post(
            url,
            json={"publicKeyHex": public_key_hex, "signingKeyId": signing_key_id},
        ) as resp:
            if resp.status != 200:
                raise PairingError(f"start failed: HTTP {resp.status}")
            return await resp.json()

    async def get_status(self, secret: str) -> PairingPending | PairingClaimed:
        """GET /api/v1/ha-pairing/:secret/status. Raises PairingExpired / PairingNotFound."""
        url = f"{self._base}/api/v1/ha-pairing/{secret}/status"
        async with self._session.get(url) as resp:
            if resp.status == 404:
                raise PairingNotFound("pairing not found")
            if resp.status == 410:
                raise PairingExpired("pairing expired")
            if resp.status != 200:
                raise PairingError(f"status failed: HTTP {resp.status}")
            body = await resp.json()
            if body["status"] == "pending":
                return PairingPending()
            if body["status"] == "claimed":
                return PairingClaimed(
                    household_id=body["householdId"],
                    preset_id=body.get("presetId"),
                )
            raise PairingError(f"unknown status: {body['status']}")

    async def finalize(
        self, *, secret: str, public_key_hex: str, signing_key_id: str
    ) -> dict[str, Any]:
        """POST /api/v1/ha-pairing/:secret/finalize — finalize. Returns
        {edgeDeviceId, hardwareId, apiKey, householdId, presetId, trustedKeys}."""
        url = f"{self._base}/api/v1/ha-pairing/{secret}/finalize"
        async with self._session.post(
            url,
            json={
                "secret": secret,
                "publicKeyHex": public_key_hex,
                "signingKeyId": signing_key_id,
            },
        ) as resp:
            if resp.status == 404:
                raise PairingNotFound("pairing not found")
            if resp.status == 410:
                raise PairingExpired("pairing expired")
            if resp.status == 409:
                raise PairingConflict("pairing not claimed yet")
            if resp.status != 200:
                raise PairingError(f"finalize failed: HTTP {resp.status}")
            return await resp.json()
