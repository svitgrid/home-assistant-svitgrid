"""A pending pairing that the cloud or the LAN can claim, whichever is first.

The config flow shows one code. A signed-in Svitgrid app claims it through the
cloud, which the flow learns by polling `/ha-pairing/{secret}/status`. A guest
app, with no account, claims it on the LAN with `POST /api/svitgrid/pair-local`.
`PendingPairing` is the one place both paths record a claim, so the second
claimant always loses.

When the cloud cannot be reached, the flow mints the code here instead, from
the cloud's alphabet, and only the LAN can claim it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

from .bundled_presets import load_bundled_preset
from .const import (
    DOMAIN,
    LOCAL_PAIRING_MAX_WRONG_CODES,
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
)
from .inverter_entry import harvest_config_from_api

_LOGGER = logging.getLogger(__name__)

# hass.data[DOMAIN] keys. `pending_pairing` holds ONLY the code, because
# /hello publishes it unauthenticated; the session object lives apart from it.
PENDING_PAIRING_KEY = "pending_pairing"
SESSION_KEY = "local_pairing"

# How long pair-local waits for the flow to create the entry. Creating it
# includes setting the entry up, which opens the local store.
_COMPLETE_TIMEOUT_S = 30.0
_COMPLETE_RETRY_S = 0.05
# How long pair-local leaves an open browser to finish the flow itself. The
# browser reacts to the progress event by configuring the flow, and finishing
# it here first would show that browser a "flow not found" error instead of
# the new entry. With no browser open, pair-local finishes it after this.
BROWSER_GRACE_S = 3.0

_DEFAULT_COMMAND_CONFIG = {"hub_name": "solarman", "slave_id": 1, "battery_voltage": 52.8}

# The protocol whose collector dials Home Assistant: no address, and the model
# may legitimately be unknown until the collector reports it.
_EYBOND_PROTOCOL = "eybond_at"


class PairingCancelled(Exception):
    """The pending pairing ended: too many wrong codes on pair-local."""


class InvalidClaim(ValueError):
    """A pair-local body the integration cannot build a station from."""


def mint_code() -> str:
    """A pairing code in the cloud's format, for when the cloud is unreachable."""
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


@dataclass
class LanClaim:
    """What a successful pair-local hands the config flow."""

    island_key: str
    device_id: str
    device_label: str | None
    signing_key_id: str
    public_key_hex: str
    station_name: str | None
    preset_id: str | None
    inverters: list[dict[str, Any]]
    paired_at: str


@dataclass
class PendingPairing:
    """One pairing code on offer, and who has claimed it."""

    code: str
    flow_id: str | None = None
    pairing_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    wrong_codes: int = 0
    claimed_by: str | None = None  # "cloud" | "lan"
    cancelled: bool = False
    lan_claim: LanClaim | None = None

    def __post_init__(self) -> None:
        # Set on a LAN claim and on cancellation. An Event rather than a
        # Future: a waiter cancelled because the cloud won must not cancel the
        # state every other reader sees.
        self._settled = asyncio.Event()

    @property
    def is_open(self) -> bool:
        """True while a claim can still succeed."""
        return self.claimed_by is None and not self.cancelled

    def code_matches(self, code: str) -> bool:
        candidate = code.strip().upper() if isinstance(code, str) else ""
        return hmac.compare_digest(candidate.encode(), self.code.upper().encode())

    def record_wrong_code(self) -> int:
        """Count a wrong code and return the attempts left. At zero the
        pairing is cancelled."""
        self.wrong_codes += 1
        left = max(0, LOCAL_PAIRING_MAX_WRONG_CODES - self.wrong_codes)
        if left == 0:
            self.cancel()
        return left

    def claim_cloud(self) -> bool:
        """Record a cloud claim. False when the LAN claimed first."""
        if not self.is_open:
            return False
        self.claimed_by = "cloud"
        return True

    def claim_lan(self, claim: LanClaim) -> bool:
        """Record a LAN claim. False when anything claimed first."""
        if not self.is_open:
            return False
        self.claimed_by = "lan"
        self.lan_claim = claim
        self._settled.set()
        return True

    def cancel(self) -> None:
        if self.claimed_by is not None:
            return
        self.cancelled = True
        self._settled.set()

    async def wait_for_lan(self) -> LanClaim:
        """Resolve on a LAN claim; raise PairingCancelled on cancellation."""
        await self._settled.wait()
        if self.lan_claim is None:
            raise PairingCancelled("the pending pairing was cancelled")
        return self.lan_claim


def publish(hass: HomeAssistant, session: PendingPairing) -> None:
    """Offer the session's code: /hello shows it, and pair-local accepts it."""
    data = hass.data.setdefault(DOMAIN, {})
    # ONLY the code. /hello is unauthenticated; anything else here is public.
    data[PENDING_PAIRING_KEY] = {"code": session.code}
    data[SESSION_KEY] = session


def get_session(hass: HomeAssistant) -> PendingPairing | None:
    session = (hass.data.get(DOMAIN) or {}).get(SESSION_KEY)
    return session if isinstance(session, PendingPairing) else None


def close(hass: HomeAssistant, session: PendingPairing | None) -> None:
    """Stop offering the code.

    A claimed session stays, so a late pair-local hears `already_claimed`
    rather than `no_pending_pairing`. Any other session goes, so pair-local
    answers 404 from here on. Only `session` is touched: a newer pairing that
    replaced it keeps its own state.
    """
    data = hass.data.setdefault(DOMAIN, {})
    current = data.get(SESSION_KEY)
    if session is not None and current is not session:
        return
    data[PENDING_PAIRING_KEY] = None
    if session is not None and session.claimed_by is None:
        session.cancel()
        data.pop(SESSION_KEY, None)


def cancel(hass: HomeAssistant, session: PendingPairing) -> None:
    """End the pending pairing now (too many wrong codes)."""
    session.cancel()
    close(hass, session)


def build_local_inverters(raw_inverters: list[Any]) -> list[dict[str, Any]]:
    """Turn pair-local's `inverters` into config-entry inverters. Reads the
    bundled presets from disk, so run it in an executor.

    Keyed by the app's `inverterId`: readings, events and /live answers carry
    it. A preset supplies the entity map and command recipes of a relay
    inverter; a `harvestConfig` makes the inverter a direct-harvest one.
    Raises InvalidClaim on anything it cannot build.
    """
    if not isinstance(raw_inverters, list) or not raw_inverters:
        raise InvalidClaim("inverters must be a non-empty list")
    built: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_inverters:
        if not isinstance(raw, dict):
            raise InvalidClaim("each inverter must be an object")
        inverter_id = raw.get("inverterId")
        if not isinstance(inverter_id, str) or not inverter_id or len(inverter_id) > 128:
            raise InvalidClaim("inverterId must be a non-empty string")
        if inverter_id in seen:
            raise InvalidClaim(f"duplicate inverterId {inverter_id}")
        seen.add(inverter_id)
        preset_id = raw.get("presetId")
        harvest = raw.get("harvestConfig")
        if preset_id is not None and not isinstance(preset_id, str):
            raise InvalidClaim("presetId must be a string")
        if harvest is not None and not isinstance(harvest, dict):
            raise InvalidClaim("harvestConfig must be an object")
        if not preset_id and not harvest:
            raise InvalidClaim(f"inverter {inverter_id} carries neither presetId nor harvestConfig")
        preset = load_bundled_preset(preset_id) if preset_id else None
        if preset_id and preset is None and not harvest:
            raise InvalidClaim(f"unknown preset {preset_id}")
        name = raw.get("name")
        item: dict[str, Any] = {
            "inverter_id": inverter_id,
            "name": name if isinstance(name, str) else None,
            "entity_map": dict(preset["entityMap"]) if preset else {},
            "command_recipes": list(preset["commands"]) if preset else [],
            "command_config": dict(_DEFAULT_COMMAND_CONFIG),
            "brand": preset.get("brand") if preset else None,
            "model": preset.get("model") if preset else None,
            "phases": preset.get("phases") if preset else None,
            "has_battery": preset.get("hasBattery") if preset else None,
            "pv_strings": preset.get("pvStrings") if preset else None,
            "preset_id": preset_id or None,
        }
        if preset is not None:
            # Same field the preset refresh writes, so a later refresh from the
            # cloud does not re-merge what is already here.
            item["merged_preset_version"] = preset["version"]
        if harvest:
            config = harvest_config_from_api(harvest)
            model_id = config.get("model_id")
            if config.get("protocol") != _EYBOND_PROTOCOL and (
                not isinstance(model_id, str) or not model_id
            ):
                raise InvalidClaim(f"harvestConfig of {inverter_id} has no modelId")
            item["harvest_config"] = config
        built.append(item)
    return built


def _entry_for(hass: HomeAssistant, session: PendingPairing) -> str | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("local_pairing_id") == session.pairing_id:
            return entry.entry_id
    return None


async def complete_flow(hass: HomeAssistant, session: PendingPairing) -> str | None:
    """Drive the config flow to its entry and return the entry id.

    Home Assistant finishes a progress step only when something calls
    `async_configure`, normally the browser showing the code. A guest pairing
    must finish with nobody at that screen, so pair-local does it, after
    giving an open browser BROWSER_GRACE_S to do it first. A still-progressing
    flow is retried briefly. If the browser finished the flow, the entry is
    found by the pairing id it carries.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _COMPLETE_TIMEOUT_S
    grace_end = loop.time() + BROWSER_GRACE_S
    while loop.time() < grace_end:
        entry_id = _entry_for(hass, session)
        if entry_id is not None:
            return entry_id
        await asyncio.sleep(_COMPLETE_RETRY_S)
    flow_id = session.flow_id
    while loop.time() < deadline:
        if flow_id is None:
            break
        try:
            result = await hass.config_entries.flow.async_configure(flow_id)
        except UnknownFlow:
            flow_id = None
            break
        if result["type"] == FlowResultType.CREATE_ENTRY:
            return result["result"].entry_id
        if result["type"] == FlowResultType.ABORT:
            break
        await asyncio.sleep(_COMPLETE_RETRY_S)
    while loop.time() < deadline:
        entry_id = _entry_for(hass, session)
        if entry_id is not None:
            return entry_id
        await asyncio.sleep(_COMPLETE_RETRY_S)
    return _entry_for(hass, session)


_ISLAND_KEY = re.compile(r"^[A-Za-z0-9_-]{43,512}$")


def parse_identity(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the non-key fields of a pair-local body. Raises InvalidClaim."""
    island_key = body.get("islandKey")
    if not isinstance(island_key, str) or not _ISLAND_KEY.match(island_key):
        raise InvalidClaim("islandKey must be base64url, 43 characters or more")
    device_id = body.get("deviceId")
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise InvalidClaim("deviceId must be a non-empty string")
    device_label = body.get("deviceLabel")
    if device_label is not None and not isinstance(device_label, str):
        raise InvalidClaim("deviceLabel must be a string")
    station_name = body.get("stationName")
    if station_name is not None and not isinstance(station_name, str):
        raise InvalidClaim("stationName must be a string")
    preset_id = body.get("presetId")
    if preset_id is not None and not isinstance(preset_id, str):
        raise InvalidClaim("presetId must be a string")
    for key in ("signingKeyId", "publicKeyHex", "signature"):
        if not isinstance(body.get(key), str) or not body.get(key):
            raise InvalidClaim(f"{key} must be a non-empty string")
    return {
        "island_key": island_key,
        "device_id": device_id,
        "device_label": (device_label or "").strip()[:100] or None,
        "station_name": (station_name or "").strip()[:100] or None,
        "preset_id": preset_id or None,
    }
