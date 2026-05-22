"""Svitgrid config flow.

Phase 1: Pair branch only. Manual branch ships in Phase 4.
"""
from __future__ import annotations

import asyncio
import logging
from secrets import token_hex
from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client

from .const import (
    DEFAULT_API_BASE,
    DOMAIN,
    PAIRING_MAX_POLL_DURATION_S,
    PAIRING_POLL_INTERVAL_S,
)
from .pairing_client import (
    PairingClaimed,
    PairingClient,
    PairingError,
    PairingExpired,
    PairingPending,
)
from .signing import generate_keypair, serialize_private_key

_LOGGER = logging.getLogger(__name__)


class SvitgridConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Svitgrid setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._secret: str | None = None
        self._code: str | None = None
        self._private_key = None  # ec.EllipticCurvePrivateKey
        self._public_key_hex: str | None = None
        self._signing_key_id: str | None = None
        self._pair_task: asyncio.Task | None = None
        self._final_payload: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First step — present Pair vs Manual."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["pair", "manual"],
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manual entity-mapping branch — Phase 4."""
        return self.async_abort(reason="manual_branch_not_implemented")

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Kick off pairing: generate keypair, /start, then show waiting + poll.

        This step is also called again by HA when the polling task completes.
        """
        if not self._pair_task:
            # First entry — generate keys, call /start, create background task.
            session = aiohttp_client.async_get_clientsession(self.hass)
            client = PairingClient(session, api_base=DEFAULT_API_BASE)

            self._private_key, self._public_key_hex = generate_keypair()
            self._signing_key_id = f"ha-{token_hex(4)}"

            try:
                start_result = await client.start(
                    public_key_hex=self._public_key_hex,
                    signing_key_id=self._signing_key_id,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Pairing /start failed")
                return self.async_abort(reason="cannot_connect")

            self._secret = start_result["secret"]
            self._code = start_result["code"]
            self._pair_task = self.hass.async_create_task(
                self._poll_for_claim(client)
            )

        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="waiting_for_mobile",
                progress_task=self._pair_task,
                description_placeholders={"code": self._code},
            )

        # Task finished — check for errors.
        try:
            await self._pair_task
        except PairingExpired:
            return self.async_abort(reason="pairing_expired")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Pairing polling failed")
            return self.async_abort(reason="pairing_failed")
        finally:
            self._pair_task = None

        return self.async_show_progress_done(next_step_id="pair_finalize")

    async def async_step_pair_finalize(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Polling saw 'claimed'; create the entry."""
        if self._final_payload is None:
            return self.async_abort(reason="pairing_failed")

        # Phase 2: persist preset metadata returned by /finalize so
        # async_setup_entry can boot the readings publisher with a working
        # entityMap without any extra round-trip. Translate the API's
        # camelCase to HA's snake_case convention.
        return self.async_create_entry(
            title=self._entry_title(),
            data={
                "api_base": DEFAULT_API_BASE,
                "api_key": self._final_payload["apiKey"],
                "edge_device_id": self._final_payload["edgeDeviceId"],
                "hardware_id": self._final_payload["hardwareId"],
                "household_id": self._final_payload["householdId"],
                "signing_key_id": self._signing_key_id,
                "private_key_pem": serialize_private_key(self._private_key),
                "public_key_hex": self._public_key_hex,
                "trusted_keys": self._final_payload["trustedKeys"],
                "preset_id": self._final_payload.get("presetId"),
                # Phase 2 fields (None when /finalize had no preset).
                "entity_map": self._final_payload.get("entityMap") or {},
                "brand": self._final_payload.get("brand"),
                "model": self._final_payload.get("model"),
                "phases": self._final_payload.get("phases"),
                "has_battery": self._final_payload.get("hasBattery"),
                "pv_strings": self._final_payload.get("pvStrings"),
                # Phase 2-advanced: write-command recipes for YamlDispatcher.
                "commands": self._final_payload.get("commands") or [],
            },
        )

    def _entry_title(self) -> str:
        """Brand+model when known; falls back to householdId for bare pairings."""
        brand = self._final_payload.get("brand")
        model = self._final_payload.get("model")
        if brand and model:
            return f"Svitgrid — {brand} {model}"
        return f"Svitgrid ({self._final_payload['householdId']})"

    async def _poll_for_claim(self, client: PairingClient) -> None:
        """Background task — polls /status until claimed, then calls /finalize."""
        deadline = self.hass.loop.time() + PAIRING_MAX_POLL_DURATION_S
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(PAIRING_POLL_INTERVAL_S)
            try:
                status = await client.get_status(self._secret)
            except PairingExpired:
                raise
            except PairingError as exc:
                _LOGGER.warning("status poll failed: %s; retrying", exc)
                continue
            if isinstance(status, PairingPending):
                continue
            if isinstance(status, PairingClaimed):
                self._final_payload = await client.finalize(
                    secret=self._secret,
                    public_key_hex=self._public_key_hex,
                    signing_key_id=self._signing_key_id,
                )
                return
        raise PairingExpired("pairing window expired during polling")
