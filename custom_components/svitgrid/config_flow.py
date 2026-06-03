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
from homeassistant.core import callback
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

from .api_client import SvitgridApiClient
from .const import (
    DEFAULT_API_BASE,
    DOMAIN,
    MAPPABLE_FIELDS,
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

# Every Svitgrid reading field the publisher knows about, sourced from the
# single canonical list in const.py so the manual pairing form and the options
# (edit) form share one definition. Manual mode offers an EntitySelector per
# field; the user may leave any blank. At least one must be set (validated at
# form submit in async_step_manual_entities).
_MANUAL_FIELDS = tuple(MAPPABLE_FIELDS)


class SvitgridConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Svitgrid setup."""

    VERSION = 2
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SvitgridOptionsFlow":
        """Expose the 'Configure' button so users can edit sensor mappings."""
        return SvitgridOptionsFlow(config_entry)

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
        #
        # The entry is created at VERSION 2, so async_migrate_entry (which wraps
        # a legacy flat entity_map into `inverters`) never runs for a fresh
        # pairing. We MUST write the v2 `inverters` list here — otherwise
        # `_inverters_from_entry` returns [] and the readings publisher never
        # starts ("no inverters configured; nothing to publish"). The flat
        # fields below are kept for back-compat; the inverters list is
        # authoritative.
        inverter = {
            "inverter_id": self._final_payload["hardwareId"],
            "entity_map": self._final_payload.get("entityMap") or {},
            "command_recipes": self._final_payload.get("commands") or [],
            "command_config": {"hub_name": "solarman", "slave_id": 1, "battery_voltage": 52.8},
            "brand": self._final_payload.get("brand"),
            "model": self._final_payload.get("model"),
            "phases": self._final_payload.get("phases"),
            "has_battery": self._final_payload.get("hasBattery"),
            "pv_strings": self._final_payload.get("pvStrings"),
            "preset_id": self._final_payload.get("presetId"),
        }
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
                # Canonical v2 shape read by _inverters_from_entry.
                "inverters": [inverter],
                # Phase 2 flat fields (None when /finalize had no preset) — kept
                # for back-compat; the inverters list above is authoritative.
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


class SvitgridOptionsFlow(config_entries.OptionsFlow):
    """Add / edit / remove inverters on an already-paired add-on."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._add_meta: dict[str, Any] | None = None
        self._edit_inverter_id: str | None = None

    # ── menu ────────────────────────────────────────────────────────────
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_inverter", "edit_inverter", "remove_inverter"],
        )

    def _inverters(self) -> list[dict[str, Any]]:
        return [dict(i) for i in (self._entry.data.get("inverters") or [])]

    def _persist_inverters(self, inverters: list[dict[str, Any]]) -> None:
        new_data = {**self._entry.data, "inverters": inverters}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)

    # ── add: step 1 (brand/model metadata) ──────────────────────────────
    async def async_step_add_inverter(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._add_meta = {
                "brand": user_input["brand"].strip(),
                "model": user_input["model"].strip(),
                "phases": int(user_input["phases"]),
                "hasBattery": bool(user_input["has_battery"]),
                "pvStrings": int(user_input["pv_strings"]),
            }
            return await self.async_step_add_inverter_entities()
        schema = vol.Schema({
            vol.Required("brand"): TextSelector(TextSelectorConfig()),
            vol.Required("model"): TextSelector(TextSelectorConfig()),
            vol.Required("phases", default="3"): SelectSelector(SelectSelectorConfig(options=["1", "2", "3"])),
            vol.Required("has_battery", default=True): BooleanSelector(),
            vol.Required("pv_strings", default=2): NumberSelector(
                NumberSelectorConfig(min=1, max=8, step=1, mode=NumberSelectorMode.BOX)),
        })
        return self.async_show_form(step_id="add_inverter", data_schema=schema)

    # ── add: step 2 (map sensors + write targets, call API, append) ──────
    async def async_step_add_inverter_entities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            hub_name = user_input.get("hub_name", "solarman")
            slave_id = int(user_input.get("slave_id", 1))
            entity_map = {f: eid for f, eid in user_input.items() if eid and f not in ("hub_name", "slave_id")}
            if not entity_map:
                errors["base"] = "no_entities_selected"
            else:
                if self._add_meta is None:
                    return self.async_abort(reason="inverter_not_found")
                session = aiohttp_client.async_get_clientsession(self.hass)
                client = SvitgridApiClient(session, api_base=self._entry.data["api_base"])
                try:
                    resp = await client.add_inverter(
                        api_key=self._entry.data["api_key"],
                        inverter={**self._add_meta, "entityMap": entity_map, "commands": []},
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("add_inverter API call failed")
                    return self.async_abort(reason="cannot_connect")
                inverters = self._inverters()
                inverters.append({
                    "inverter_id": resp["inverterId"],
                    "entity_map": entity_map,
                    "command_recipes": resp.get("commands") or [],
                    # TODO: battery_voltage is hardcoded to a 48V nominal; make
                    # configurable for 24V/12V systems (matches migration default).
                    "command_config": {"hub_name": hub_name, "slave_id": slave_id, "battery_voltage": 52.8},
                    "brand": resp.get("brand"), "model": resp.get("model"),
                    "phases": resp.get("phases"), "has_battery": resp.get("hasBattery"),
                    "pv_strings": resp.get("pvStrings"), "preset_id": resp.get("presetId"),
                })
                self._persist_inverters(inverters)
                # The data write in _persist_inverters already triggers the reload
                # listener; return options unchanged so we don't reload twice or wipe
                # existing options.
                return self.async_create_entry(title="", data=dict(self._entry.options))

        schema_dict: dict[Any, Any] = {}
        for field, _label in _MANUAL_FIELDS:
            schema_dict[vol.Optional(field)] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema_dict[vol.Optional("hub_name", default="solarman")] = TextSelector(TextSelectorConfig())
        schema_dict[vol.Optional("slave_id", default=1)] = NumberSelector(
            NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX))
        return self.async_show_form(
            step_id="add_inverter_entities", data_schema=vol.Schema(schema_dict), errors=errors)

    # ── edit: pick inverter, then re-map (scoped to the selection) ───────
    async def async_step_edit_inverter(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        inverters = self._inverters()
        if user_input is not None and "inverter_id" in user_input and self._edit_inverter_id is None:
            self._edit_inverter_id = user_input["inverter_id"]
            return await self.async_step_edit_inverter()
        if self._edit_inverter_id is None:
            options = [{"value": i["inverter_id"], "label": f'{i.get("brand") or "?"} {i.get("model") or "?"} ({i["inverter_id"]})'} for i in inverters]
            return self.async_show_form(
                step_id="edit_inverter",
                data_schema=vol.Schema({vol.Required("inverter_id"): SelectSelector(SelectSelectorConfig(options=options))}))
        target = next((i for i in inverters if i["inverter_id"] == self._edit_inverter_id), None)
        if target is None:
            return self.async_abort(reason="inverter_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = {f: eid for f, eid in user_input.items() if eid}
            if cleaned:
                target["entity_map"] = cleaned
                self._persist_inverters([target if i["inverter_id"] == self._edit_inverter_id else i for i in inverters])
                # The data write in _persist_inverters already triggers the reload
                # listener; return options unchanged so we don't reload twice or wipe
                # existing options.
                return self.async_create_entry(title="", data=dict(self._entry.options))
            errors["base"] = "no_entities_selected"
        schema = vol.Schema({vol.Optional(field): EntitySelector(EntitySelectorConfig(domain="sensor")) for field, _ in _MANUAL_FIELDS})
        return self.async_show_form(
            step_id="edit_inverter",
            data_schema=self.add_suggested_values_to_schema(schema, target.get("entity_map") or {}),
            errors=errors,
        )

    # ── remove: pick inverter, drop from list (local only) ───────────────
    async def async_step_remove_inverter(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        inverters = self._inverters()
        if user_input is not None:
            remaining = [i for i in inverters if i["inverter_id"] != user_input["inverter_id"]]
            if not remaining:
                return self.async_abort(reason="cannot_remove_last_inverter")
            self._persist_inverters(remaining)
            # The data write in _persist_inverters already triggers the reload
            # listener; return options unchanged so we don't reload twice or wipe
            # existing options.
            return self.async_create_entry(title="", data=dict(self._entry.options))
        options = [{"value": i["inverter_id"], "label": f'{i.get("brand") or "?"} {i.get("model") or "?"} ({i["inverter_id"]})'} for i in inverters]
        return self.async_show_form(
            step_id="remove_inverter",
            data_schema=vol.Schema({vol.Required("inverter_id"): SelectSelector(SelectSelectorConfig(options=options))}))
