"""Svitgrid config flow.

Two top-level branches:
  - "pair"   — preset-driven path (mobile picks brand from haPresets);
               same as Phase 1+2.
  - "manual" — user picks "I don't see my inverter" on mobile and
               collects brand/model/phases + per-field HA entities here.
               Manual flow always ends in the same pair step (show code →
               poll → finalize), with the collected metadata submitted
               in the /finalize body's `inverter:` field.
"""
from __future__ import annotations

import asyncio
import logging
from secrets import token_hex
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

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

# Every Svitgrid reading field the publisher knows about. Manual mode
# offers an EntitySelector per field; user can leave them blank.
# Required: at least one must be set (validated at form submit).
_MANUAL_FIELDS = (
    ("batterySoc", "Battery state of charge (%)"),
    ("batteryPower", "Battery power (W, signed: positive=charging)"),
    ("batteryVoltage", "Battery voltage (V)"),
    ("pv1Power", "PV string 1 power (W)"),
    ("pv2Power", "PV string 2 power (W)"),
    ("pv3Power", "PV string 3 power (W)"),
    ("pv4Power", "PV string 4 power (W)"),
    ("gridPower", "Grid power (W, signed: positive=import)"),
    ("loadPower", "Load power (W)"),
    ("dailyPvEnergy", "Daily PV production (kWh)"),
    ("gridVoltageL1", "Grid voltage L1 (V)"),
    ("gridVoltageL2", "Grid voltage L2 (V)"),
    ("gridVoltageL3", "Grid voltage L3 (V)"),
)


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
        # Manual-mode state. Stays None in preset (pair) mode; populated
        # by async_step_manual_meta → async_step_manual_entities and
        # submitted in /finalize's body when the pair completes.
        self._manual_inverter: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First step — present Pair vs Manual."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["pair", "manual"],
        )

    # ─── Manual branch (Phase 2A M3–M7) ──────────────────────────────────

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manual entry point: collect inverter metadata first."""
        return await self.async_step_manual_meta()

    async def async_step_manual_meta(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual step 1 of 2: brand / model / phases / battery / pv-strings."""
        if user_input is not None:
            self._manual_inverter = {
                "brand": user_input["brand"].strip(),
                "model": user_input["model"].strip(),
                "phases": int(user_input["phases"]),
                "hasBattery": bool(user_input["has_battery"]),
                "pvStrings": int(user_input["pv_strings"]),
                "entityMap": {},   # filled in next step
                "commands": [],    # read-only in manual mode
            }
            return await self.async_step_manual_entities()

        schema = vol.Schema({
            vol.Required("brand"): TextSelector(TextSelectorConfig()),
            vol.Required("model"): TextSelector(TextSelectorConfig()),
            vol.Required("phases", default=3): SelectSelector(
                SelectSelectorConfig(options=["1", "2", "3"]),
            ),
            vol.Required("has_battery", default=True): BooleanSelector(),
            vol.Required("pv_strings", default=2): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=8, step=1, mode=NumberSelectorMode.BOX,
                ),
            ),
        })
        return self.async_show_form(step_id="manual_meta", data_schema=schema)

    async def async_step_manual_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual step 2 of 2: pick HA entity per Svitgrid field.

        At least one entity must be chosen — otherwise the publisher has
        nothing to send and the dashboard is permanently empty."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_map = {
                field: eid for field, eid in user_input.items() if eid
            }
            if not entity_map:
                errors["base"] = "no_entities_selected"
            else:
                assert self._manual_inverter is not None
                self._manual_inverter["entityMap"] = entity_map
                # Hand off to the existing pair flow — same code-display +
                # poll-for-claim path as the preset branch.
                return await self.async_step_pair()

        # Build the form: one EntitySelector per supported Svitgrid field.
        # Defaults: empty (no entity selected).
        schema_dict: dict[Any, Any] = {}
        for field, _label in _MANUAL_FIELDS:
            schema_dict[vol.Optional(field)] = EntitySelector(
                EntitySelectorConfig(domain="sensor"),
            )
        return self.async_show_form(
            step_id="manual_entities",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    # ─── Pair branch (preset OR continuation of manual) ────────────────

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
                    # Manual-mode: hand the user-collected inverter spec to
                    # the API so it creates inverters/{hwid} with the right
                    # brand / entityMap. Preset-mode: None — API looks up
                    # the preset server-side.
                    inverter=self._manual_inverter,
                )
                return
        raise PairingExpired("pairing window expired during polling")
