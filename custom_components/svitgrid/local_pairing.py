"""A pending pairing that the cloud or the LAN can claim, and the owner decides.

The config flow shows one code. A signed-in Svitgrid app claims it through the
cloud, which the flow learns by polling `/ha-pairing/{secret}/status`. A guest
app, with no account, claims it on the LAN with `POST /api/svitgrid/pair-local`.
`PendingPairing` is the one place both paths record a claim, so the second
claimant always loses.

`/hello` publishes the code to anyone on the network, so the code alone does
not authorise a LAN claim. A LAN claim that passes every check becomes a
*candidate*: the pairing dialog asks the owner to approve it, and pair-local
waits for the answer, for up to APPROVAL_TIMEOUT_S. Only an approved candidate
claims the pairing. While a candidate waits, a cloud claim is held back: it
wins if the owner rejects, and loses (with a warning) if the owner approves.

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

# How long pair-local holds a request while the owner decides (contract).
APPROVAL_TIMEOUT_S = 120

# How long pair-local waits, after approval, for the entry to appear.
# Creating it includes setting the entry up, which opens the local store.
_ENTRY_TIMEOUT_S = 30.0
_ENTRY_RETRY_S = 0.05

_DEFAULT_COMMAND_CONFIG = {"hub_name": "solarman", "slave_id": 1, "battery_voltage": 52.8}

# The protocol whose collector dials Home Assistant: no address, and the model
# may legitimately be unknown until the collector reports it.
_EYBOND_PROTOCOL = "eybond_at"

# Why the LAN half closed.
LAN_CLOSED_WRONG_CODES = "wrong_codes"
LAN_CLOSED_CONFIGURED = "already_configured"

# The owner's answer, as pair-local receives it.
APPROVED = "approved"
REJECTED = "rejected"
GONE = "gone"  # the pairing ended while the request waited


class PairingCancelled(Exception):
    """The pairing ended: too many wrong codes, and nothing else can claim it."""


class InvalidClaim(ValueError):
    """A pair-local body the integration cannot build a station from."""


def mint_code() -> str:
    """A pairing code in the cloud's format, for when the cloud is unreachable."""
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


@dataclass
class LanClaim:
    """A LAN claim that passed every check. A candidate until approved."""

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
    """One pairing code on offer, who wants it, and who has it."""

    code: str
    flow_id: str | None = None
    pairing_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    wrong_codes: int = 0
    claimed_by: str | None = None  # "cloud" | "lan"
    # The LAN half ends on too many wrong codes, or when the install already
    # has a station; a cloud claim of the same code is unaffected.
    lan_closed: bool = False
    lan_closed_reason: str | None = None
    # A LAN claim waiting for the owner, and the approved one.
    candidate: LanClaim | None = None
    lan_claim: LanClaim | None = None
    # A cloud claim, as the /status poll returned it.
    cloud_claim: Any = None
    # Loop time the pairing window closes; pair-local refuses after it.
    expires_at: float | None = None

    def __post_init__(self) -> None:
        self._changed = asyncio.Event()
        self._decision: asyncio.Future[str] | None = None

    # ── waiting ─────────────────────────────────────────────────────────
    def _notify(self) -> None:
        self._changed.set()

    def reset_changed(self) -> None:
        self._changed.clear()

    async def wait_changed(self) -> None:
        await self._changed.wait()

    # ── state ───────────────────────────────────────────────────────────
    def window_closed(self) -> bool:
        return self.expires_at is not None and asyncio.get_running_loop().time() >= self.expires_at

    @property
    def lan_open(self) -> bool:
        """True while a LAN claim may still become a candidate."""
        return self.claimed_by is None and not self.lan_closed and self.candidate is None

    def code_matches(self, code: str) -> bool:
        candidate = code.strip().upper() if isinstance(code, str) else ""
        return hmac.compare_digest(candidate.encode(), self.code.upper().encode())

    def record_wrong_code(self) -> int:
        """Count a wrong code and return the attempts left. At zero the LAN
        half closes."""
        self.wrong_codes += 1
        left = max(0, LOCAL_PAIRING_MAX_WRONG_CODES - self.wrong_codes)
        if left == 0:
            self.close_lan(LAN_CLOSED_WRONG_CODES)
        return left

    def close_lan(self, reason: str) -> None:
        if self.lan_closed:
            return
        self.lan_closed = True
        self.lan_closed_reason = reason
        self._notify()

    # ── the LAN candidate ───────────────────────────────────────────────
    def request_approval(self, claim: LanClaim) -> asyncio.Future[str] | None:
        """Make `claim` the candidate the owner is asked about. None when the
        LAN half is closed, the pairing is claimed, or another candidate
        already waits (refused rather than queued: a queue would let a
        stranger's request be approved by a tap meant for the owner's phone)."""
        if not self.lan_open:
            return None
        self.candidate = claim
        self._decision = asyncio.get_running_loop().create_future()
        self._notify()
        return self._decision

    def _decide(self, answer: str) -> None:
        if self._decision is not None and not self._decision.done():
            self._decision.set_result(answer)
        self._decision = None
        self.candidate = None

    def approve(self, shown: LanClaim | None) -> bool:
        """The owner approved `shown`, the candidate the dialog named. False
        when it no longer waits: it timed out, was withdrawn, or another
        phone's claim took its place. Approving whatever waits now would grant
        a phone under another phone's name."""
        if shown is None or self.candidate is not shown or self.claimed_by is not None:
            return False
        self.lan_claim = self.candidate
        self.claimed_by = "lan"
        if self.cloud_claim is not None:
            _LOGGER.warning(
                "The Svitgrid cloud accepted a claim of this pairing code while "
                "the owner was approving a LAN claim. The LAN claim wins; the "
                "cloud keeps that claim until it expires, and nothing finalizes it"
            )
        self._decide(APPROVED)
        self._notify()
        return True

    def reject(self) -> None:
        """The owner rejected the waiting candidate. The pairing stays pending."""
        if self.candidate is None:
            return
        self._decide(REJECTED)
        self._settle_held_cloud_claim()
        self._notify()

    def withdraw(self, claim: LanClaim) -> None:
        """Drop `claim` as the candidate: nobody answered in time, or its
        request went away. The pairing stays pending."""
        if self.candidate is not claim:
            return
        self._decide(GONE)
        self._settle_held_cloud_claim()
        self._notify()

    # ── the cloud claim ─────────────────────────────────────────────────
    def offer_cloud_claim(self, status: Any) -> None:
        """The /status poll saw a claim. It wins unless the LAN won first; it
        is held while the owner decides on a LAN candidate."""
        if self.claimed_by == "lan":
            _LOGGER.warning(
                "The Svitgrid cloud reports a claim of this pairing code after a "
                "LAN claim won. The cloud keeps that claim until it expires, and "
                "nothing finalizes it"
            )
            return
        if self.claimed_by == "cloud":
            return
        self.cloud_claim = status
        self._settle_held_cloud_claim()
        self._notify()

    def _settle_held_cloud_claim(self) -> None:
        if self.cloud_claim is not None and self.claimed_by is None and self.candidate is None:
            self.claimed_by = "cloud"

    def end(self) -> None:
        """The pairing is over. A candidate still waiting hears GONE."""
        if self.candidate is not None:
            self._decide(GONE)
        self._notify()


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
    if session is not None:
        session.end()
        if session.claimed_by is None:
            data.pop(SESSION_KEY, None)


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


async def wait_for_entry(hass: HomeAssistant, session: PendingPairing) -> str | None:
    """The id of the entry an approved claim created, or None.

    The owner's Approve tap runs the step that creates the entry, in the
    browser's request; pair-local only waits for it to appear.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ENTRY_TIMEOUT_S
    while True:
        entry_id = _entry_for(hass, session)
        if entry_id is not None or loop.time() >= deadline:
            return entry_id
        await asyncio.sleep(_ENTRY_RETRY_S)


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
