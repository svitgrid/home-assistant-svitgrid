"""Wraps HA's Store for the add-on's persistent state: API key, keypair,
signingKeyId, the cached trustedKeyIds list, and the island API key."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import LEGACY_ISLAND_DEVICE_ID, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# Called with the trusted keys (signingKeyId -> publicKeyHex) after every write.
TrustListener = Callable[[dict[str, str]], None]

# The roster label for the phone that paired this add-on. The pairing claim
# carries the phone's island key but no device id or label, so the add-on names
# the device itself.
PAIRING_ISLAND_DEVICE_LABEL = "Phone used for setup"

# Config-entry data key listing the island roster ids that entry adopted at
# pairing. Removing the entry revokes exactly these (issue #6).
ENTRY_ISLAND_DEVICE_IDS = "island_device_ids"

# Config-entry data key listing the signing key ids a LAN pairing trusted for
# that entry. Removing the entry untrusts exactly these.
ENTRY_TRUSTED_KEY_IDS = "trusted_key_ids_granted"

# Roster ids minted by `pairing_island_device_id` start with this.
PAIRING_ISLAND_DEVICE_ID_PREFIX = "paired-"


def generate_island_key() -> str:
    """Return a new random URL-safe island API key (≥32 chars)."""
    return secrets.token_urlsafe(32)


def pairing_island_device_id(key: str) -> str:
    """Return the roster device id for a key that arrived through pairing.

    Derived from the key, so adopting the same key again finds the same entry
    instead of adding a duplicate. A truncated SHA-256 names the key without
    revealing it.
    """
    return PAIRING_ISLAND_DEVICE_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _normalise_island_entry(value: Any) -> dict[str, Any] | None:
    """Coerce a stored island_keys value into the rich entry shape.

    0.16.0 stored a bare `key` string and is already released, so a real box in
    the field has that shape and must keep working.  Anything unrecognisable is
    dropped rather than raising — a corrupt blob must not lock out every device.
    """
    if isinstance(value, str):
        return {"key": value, "label": None, "pairedAt": None} if value else None
    if isinstance(value, dict):
        key = value.get("key")
        if isinstance(key, str) and key:
            label = value.get("label")
            paired_at = value.get("pairedAt")
            return {
                "key": key,
                "label": label if isinstance(label, str) else None,
                "pairedAt": paired_at if isinstance(paired_at, str) else None,
            }
    return None


# How long a pairing code issued for an edge box stays valid.
EDGE_PAIR_CODE_TTL = timedelta(seconds=600)


def _same_key(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(a, b)
    except (TypeError, ValueError):
        return False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash_pair_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass
class EdgeBoxEntry:
    """An edge box adopted by this integration, keyed by its device id.

    `api_key` is None until the box presents the pairing code issued for it.
    The code itself is never stored: only its SHA-256 hex and its expiry.
    """

    device_id: str
    api_key: str | None
    inverter_ids: list[str]
    # inverter id -> the adopt payload's per-inverter dict.
    inverters: dict[str, dict]
    added_at: str
    pair_code_hash: str | None
    pair_code_expires_at: str | None


def _parse_edge_box(device_id: str, value: Any) -> EdgeBoxEntry | None:
    """Build an entry from its stored dict, or None for anything malformed.

    A corrupt entry is dropped rather than raising, so it cannot lock out the
    rest of the keystore.
    """
    if not isinstance(value, dict) or not device_id:
        return None

    def _opt_str(key: str) -> str | None:
        v = value.get(key)
        return v if isinstance(v, str) and v else None

    inverters = value.get("inverters")
    return EdgeBoxEntry(
        device_id=device_id,
        api_key=_opt_str("api_key"),
        inverter_ids=[i for i in list(value.get("inverter_ids") or []) if isinstance(i, str)],
        inverters={str(k): dict(v) for k, v in (inverters or {}).items() if isinstance(v, dict)}
        if isinstance(inverters, dict)
        else {},
        added_at=value.get("added_at") if isinstance(value.get("added_at"), str) else "",
        pair_code_hash=_opt_str("pair_code_hash"),
        pair_code_expires_at=_opt_str("pair_code_expires_at"),
    )


@dataclass
class KeystoreState:
    api_key: str
    public_key_hex: str
    private_key_pem: str
    signing_key_id: str
    trusted_key_ids: list[str]
    trusted_public_keys_hex: dict[str, str]
    island_key: str | None = None
    # deviceId -> {"key": str, "label": str|None, "pairedAt": str|None}.  One
    # entry per paired app instance.  Replaces the single-slot `island_key`,
    # which is retained read-only so a box upgraded from the old scheme keeps
    # its existing device authorized.  0.16.0 stored the bare key string as
    # the value (see `_normalise_island_entry` / `load()`), so entries loaded
    # from a real box may still arrive in that shape and are migrated on read.
    island_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    # signingKeyId -> island roster deviceId, for a phone that paired over the
    # LAN: which roster row a signing key belongs to. `POST
    # /api/svitgrid/keys/revoke` names a signing key and must take the phone's
    # island key with it. A view-only phone's key is recorded here too, though
    # it is never trusted. Keys trusted through the cloud have no row.
    key_devices: dict[str, str] = field(default_factory=dict)
    # Signing key ids revoked over the LAN. The cloud never hears of such a
    # revoke, so its approved list still names the key; every write drops
    # these ids from the trusted keys, so no resync can trust one again. Only
    # a LAN pairing that the owner approves clears an id (`async_trust_key`).
    revoked_key_ids: list[str] = field(default_factory=list)
    # deviceId -> the edge boxes adopted for an edge island. Any number of
    # boxes; a keystore written before 0.26.0 has none.
    edge_boxes: dict[str, EdgeBoxEntry] = field(default_factory=dict)
    # signingKeyId -> the edge boxes that do not hold the key yet. A key
    # trusted over the LAN while a box is adopted is pending on every box: the
    # integration cannot sign the box's `add_trusted_key`, so a phone the box
    # already trusts has to (`/edge/keys/add`). A box drops off its key's list
    # when it acks the `add_trusted_key` with success.
    box_pending_keys: dict[str, list[str]] = field(default_factory=dict)

    def edge_boxes_list(self) -> list[EdgeBoxEntry]:
        """Every adopted edge box, in insertion order."""
        return list(self.edge_boxes.values())

    def edge_box_for_api_key(self, api_key: str) -> EdgeBoxEntry | None:
        """The box bound to `api_key`, or None.

        Compares against every bound entry in constant time and does not stop
        at the first match, so the timing says nothing about which key matched.
        """
        if not api_key:
            return None
        found: EdgeBoxEntry | None = None
        presented = api_key.encode("utf-8")
        for entry in self.edge_boxes.values():
            if entry.api_key is None:
                continue
            if hmac.compare_digest(entry.api_key.encode("utf-8"), presented) and found is None:
                found = entry
        return found

    def edge_box_for_pair_code(
        self, code: str
    ) -> tuple[EdgeBoxEntry | None, Literal["ok", "invalid", "expired"]]:
        """The unbound box a pairing code was issued for.

        Returns the box and "ok", or None with "invalid" (no unbound box holds
        this code) or "expired" (one does, but its ten minutes have passed).
        """
        if not code:
            return None, "invalid"
        presented = _hash_pair_code(code)
        match: EdgeBoxEntry | None = None
        for entry in self.edge_boxes.values():
            if entry.api_key is not None or entry.pair_code_hash is None:
                continue
            if hmac.compare_digest(entry.pair_code_hash, presented) and match is None:
                match = entry
        if match is None:
            return None, "invalid"
        try:
            expires_at = datetime.fromisoformat(match.pair_code_expires_at or "")
        except ValueError:
            return None, "expired"
        if _utcnow() >= expires_at:
            return None, "expired"
        return match, "ok"

    def edge_box_issued_pair_code(self, code: str) -> EdgeBoxEntry | None:
        """The unbound box `code` was issued to, live or expired, or None.
        Names the box a refused code belongs to; it never authenticates."""
        if not code:
            return None
        presented = _hash_pair_code(code)
        match: EdgeBoxEntry | None = None
        for entry in self.edge_boxes.values():
            if entry.api_key is not None or entry.pair_code_hash is None:
                continue
            if hmac.compare_digest(entry.pair_code_hash, presented) and match is None:
                match = entry
        return match

    def all_island_keys(self) -> list[str]:
        """Every currently-valid island key: the per-device map plus the
        legacy scalar.  Order is stable (map first, insertion order), and
        duplicates are collapsed so re-pairing the same device is a no-op.

        Returns KEYS, not entries — `island_auth.py` and every `http_views.py`
        auth call site depend on this signature.  Malformed entries are skipped
        rather than raising: a corrupt blob must not lock every device out.
        """
        keys: list[str] = []
        for entry in self.island_keys.values():
            value = entry.get("key") if isinstance(entry, dict) else None
            if value and value not in keys:
                keys.append(value)
        if self.island_key and self.island_key not in keys:
            keys.append(self.island_key)
        return keys

    def device_for_island_key(self, key: str) -> str | None:
        """The roster device id that holds `key`, or None.

        Compares every stored key with `hmac.compare_digest` and never stops at
        the first match, as `island_auth.island_key_present_and_valid` does. The
        legacy scalar maps to `LEGACY_ISLAND_DEVICE_ID`.
        """
        if not key:
            return None
        found: str | None = None
        for device_id, entry in self.island_keys.items():
            value = entry.get("key") if isinstance(entry, dict) else None
            if value and _same_key(key, value) and found is None:
                found = device_id
        if self.island_key and _same_key(key, self.island_key) and found is None:
            found = LEGACY_ISLAND_DEVICE_ID
        return found

    def lan_paired_trusted_keys(self) -> dict[str, str]:
        """The trusted keys of phones paired over the LAN.

        A key counts when it is trusted, is linked to a roster row that still
        exists, and is not revoked. The cloud never hears of a LAN pairing, so
        its approved list lacks these keys, and a resync that replaces the
        trusted keys with that list must keep them. A view-only phone's key
        is linked but never trusted, so it is not here.
        """
        revoked = set(self.revoked_key_ids)
        return {
            key_id: public_key_hex
            for key_id, public_key_hex in self.trusted_public_keys_hex.items()
            if key_id not in revoked and self.key_devices.get(key_id) in self.island_keys
        }

    def load_private_key(self) -> ec.EllipticCurvePrivateKey:
        return serialization.load_pem_private_key(
            self.private_key_pem.encode("ascii"), password=None
        )  # type: ignore[return-value]


class SvitgridKeystore:
    """Async wrapper around HA's Store for add-on state.

    Stored shape:
      {
        "api_key": "...",
        "public_key_hex": "04...",
        "private_key_pem": "-----BEGIN PRIVATE KEY-----...",
        "signing_key_id": "ha-...",
        "trusted_key_ids": ["keyId1", "keyId2"],
        "trusted_public_keys_hex": {"keyId1": "04...", "keyId2": "04..."},
        "island_key": "<url-safe token or null>",
        "edge_boxes": {"<deviceId>": {"device_id": "...", "api_key": null, ...}}
      }
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._trust_listeners: list[TrustListener] = []

    def subscribe_trust(self, listener: TrustListener) -> Callable[[], None]:
        """Call `listener` with the trusted keys after every write.

        The command poller keeps the trusted keys in memory. Without this, a
        key revoked over the LAN stayed trusted there until the entry reloaded.
        Only writes through this instance notify, so code that changes the
        trusted keys uses the shared instance in `hass.data[DOMAIN]["keystore"]`.
        Returns the function that unsubscribes.
        """
        self._trust_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._trust_listeners:
                self._trust_listeners.remove(listener)

        return _unsubscribe

    async def _write(self, state: KeystoreState) -> None:
        """Persist `state` and tell the trust listeners.

        The one place a keystore write happens. It drops every revoked id from
        the trusted keys first, so a revoked key is never stored as trusted,
        whichever caller wrote it.
        """
        revoked = set(state.revoked_key_ids)
        if revoked & set(state.trusted_public_keys_hex):
            state.trusted_public_keys_hex = {
                k: v for k, v in state.trusted_public_keys_hex.items() if k not in revoked
            }
        state.trusted_key_ids = [k for k in state.trusted_key_ids if k not in revoked]
        await self._store.async_save(asdict(state))
        # getattr: tests build an instance with __new__ and a fake store.
        for listener in list(getattr(self, "_trust_listeners", ())):
            try:
                listener(dict(state.trusted_public_keys_hex))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("A trusted-key listener failed")

    async def load(self) -> KeystoreState | None:
        data = await self._store.async_load()
        if not data:
            return None
        return KeystoreState(
            api_key=data["api_key"],
            public_key_hex=data["public_key_hex"],
            private_key_pem=data["private_key_pem"],
            signing_key_id=data["signing_key_id"],
            trusted_key_ids=list(data.get("trusted_key_ids", [])),
            trusted_public_keys_hex=dict(data.get("trusted_public_keys_hex", {})),
            island_key=data.get("island_key"),
            island_keys={
                did: entry
                for did, entry in (
                    (did, _normalise_island_entry(val))
                    for did, val in dict(data.get("island_keys", {})).items()
                )
                if entry is not None
            },
            key_devices={
                str(k): v
                for k, v in dict(data.get("key_devices") or {}).items()
                if isinstance(v, str) and v
            },
            revoked_key_ids=[
                k for k in list(data.get("revoked_key_ids") or []) if isinstance(k, str) and k
            ],
            edge_boxes={
                did: entry
                for did, entry in (
                    (str(did), _parse_edge_box(str(did), val))
                    for did, val in dict(data.get("edge_boxes") or {}).items()
                )
                if entry is not None
            },
            box_pending_keys={
                str(k): [d for d in v if isinstance(d, str) and d]
                for k, v in dict(data.get("box_pending_keys") or {}).items()
                if isinstance(v, list)
            },
        )

    async def save(
        self,
        *,
        api_key: str,
        public_key_hex: str,
        private_key_pem: str,
        signing_key_id: str,
        trusted_key_ids: list[str],
        trusted_public_keys_hex: dict[str, str] | None = None,
        island_key: str | None = None,
        island_keys: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        # Preserve the currently stored island_key when the caller does not
        # explicitly pass one (e.g. re-pairing / key-rotation flows that have
        # no reason to touch the island key).  Passing island_key= explicitly
        # always wins, including passing None to clear it intentionally — but
        # since None is also the default sentinel, we resolve by reading the
        # current store value when the caller omits the argument.
        resolved_island_key = island_key
        if resolved_island_key is None:
            current = await self.load()
            if current is not None:
                resolved_island_key = current.island_key
        resolved_island_keys = island_keys
        if resolved_island_keys is None:
            current = await self.load()
            if current is not None:
                resolved_island_keys = current.island_keys
        # Never passed by a caller: setup re-saves the identity on every start,
        # and the phone-to-key links must survive that.
        # The revoked ids likewise: a restart must not forget a revoke.
        current = await self.load()
        resolved_key_devices = current.key_devices if current is not None else {}
        resolved_revoked = current.revoked_key_ids if current is not None else []
        resolved_edge_boxes = current.edge_boxes if current is not None else {}
        resolved_box_pending = current.box_pending_keys if current is not None else {}
        await self._write(
            KeystoreState(
                api_key=api_key,
                public_key_hex=public_key_hex,
                private_key_pem=private_key_pem,
                signing_key_id=signing_key_id,
                trusted_key_ids=trusted_key_ids,
                trusted_public_keys_hex=trusted_public_keys_hex or {},
                island_key=resolved_island_key,
                island_keys=resolved_island_keys or {},
                key_devices=resolved_key_devices,
                revoked_key_ids=resolved_revoked,
                edge_boxes=resolved_edge_boxes,
                box_pending_keys=resolved_box_pending,
            )
        )

    async def async_add_edge_box(self, entry: EdgeBoxEntry) -> None:
        """Store `entry`, replacing any entry with the same device id. A no-op
        on an empty keystore."""
        current = await self.load()
        if current is None:
            return
        current.edge_boxes = {**current.edge_boxes, entry.device_id: entry}
        await self._write(current)

    async def async_remove_edge_box(self, device_id: str) -> bool:
        """Forget an edge box. Returns whether it was there; idempotent."""
        current = await self.load()
        if current is None or device_id not in current.edge_boxes:
            return False
        current.edge_boxes = {k: v for k, v in current.edge_boxes.items() if k != device_id}
        await self._write(current)
        return True

    async def async_issue_pair_code(self, device_id: str) -> str:
        """Mint a pairing code for an adopted box and return it, once.

        Only the code's SHA-256 hex and its expiry, ten minutes on, are stored;
        a new code replaces the box's previous one. Raises KeyError for a box
        that is not adopted.
        """
        from .local_pairing import mint_code  # local_pairing imports this module

        current = await self.load()
        if current is None or device_id not in current.edge_boxes:
            raise KeyError(device_id)
        code = mint_code()
        entry = current.edge_boxes[device_id]
        entry.pair_code_hash = _hash_pair_code(code)
        entry.pair_code_expires_at = (_utcnow() + EDGE_PAIR_CODE_TTL).isoformat()
        await self._write(current)
        return code

    async def async_bind_edge_box(self, device_id: str, api_key: str) -> None:
        """Bind the box's API key and clear its pairing code, so the code is
        single use. Raises KeyError for a box that is not adopted."""
        current = await self.load()
        if current is None or device_id not in current.edge_boxes:
            raise KeyError(device_id)
        entry = current.edge_boxes[device_id]
        entry.api_key = api_key
        entry.pair_code_hash = None
        entry.pair_code_expires_at = None
        await self._write(current)

    async def update_trusted_keys(self, trusted_key_ids: list[str]) -> None:
        """Used by command_poller when it receives add_trusted_key / revoke_trusted_key."""
        current = await self.load()
        if current is None:
            return
        current.trusted_key_ids = trusted_key_ids
        await self._write(current)

    async def update_trusted_keys_hex(self, trusted_public_keys_hex: dict[str, str]) -> None:
        """Replace the trusted-keys cache atomically. Keeps trusted_key_ids in
        sync (derived as the dict's keys)."""
        current = await self.load()
        if current is None:
            return
        current.trusted_public_keys_hex = dict(trusted_public_keys_hex)
        current.trusted_key_ids = sorted(trusted_public_keys_hex.keys())
        await self._write(current)

    async def async_get_island_key(self) -> str | None:
        """Return the stored island API key, or None if not yet set."""
        current = await self.load()
        if current is None:
            return None
        return current.island_key

    async def async_set_island_key(self, key: str) -> None:
        """Persist the island API key, preserving all other keystore fields."""
        current = await self.load()
        if current is None:
            return
        current.island_key = key
        await self._write(current)

    async def async_add_island_key(
        self,
        device_id: str,
        key: str,
        label: str | None = None,
        paired_at: str | None = None,
    ) -> None:
        """Authorize `key` for `device_id`, leaving every other device's key
        intact.

        This is the multi-device replacement for `async_set_island_key`.  The
        old method overwrote the single slot, which silently revoked whichever
        device had paired previously — the app then 401s forever with no way to
        recover except re-pairing, which in turn revoked the other device.

        `label` and `paired_at` are optional so an older app (which sends
        neither) still produces a valid, revocable entry.
        """
        current = await self.load()
        if current is None:
            return
        current.island_keys = {
            **current.island_keys,
            device_id: {"key": key, "label": label, "pairedAt": paired_at},
        }
        await self._write(current)

    async def async_adopt_pairing_island_key(
        self,
        key: str,
        *,
        label: str = PAIRING_ISLAND_DEVICE_LABEL,
        paired_at: str | None = None,
    ) -> str | None:
        """Store a pairing-time island key as a named `island_keys` entry.

        Pairing used to write the key into the legacy `island_key` scalar, which
        the roster reports as an unidentifiable pre-0.16.0 device. When the
        scalar holds this same key, it moves into the map, so the phone
        presenting it keeps authenticating. A scalar holding a different key
        came from a genuinely old `enable_island` and stays the legacy row.

        Idempotent: a key that any entry already holds is not added again, and
        that entry keeps its label and pairing time. A no-op on an empty
        keystore, like `async_add_island_key`.

        Returns the roster id that holds the key afterwards, so the config entry
        can record it and revoke it on removal, or None on an empty keystore.
        """
        current = await self.load()
        if current is None:
            return None
        changed = False
        holder = next(
            (did for did, entry in current.island_keys.items() if entry.get("key") == key),
            None,
        )
        if holder is None:
            holder = pairing_island_device_id(key)
            current.island_keys = {
                **current.island_keys,
                holder: {
                    "key": key,
                    "label": label,
                    "pairedAt": paired_at,
                },
            }
            changed = True
        if current.island_key == key:
            current.island_key = None
            changed = True
        if changed:
            await self._write(current)
        return holder

    async def async_revoke_island_keys(
        self, device_ids: list[str], *, key: str | None = None
    ) -> list[str]:
        """Revoke every roster id in `device_ids` in one write.

        The legacy scalar is cleared only when it is this caller's key: equal to
        `key`, or a key whose pairing id is among `device_ids`. A scalar holding
        any other key came from somewhere else and stays. Returns the ids that
        were actually removed. Idempotent, and a no-op on an empty keystore.
        """
        current = await self.load()
        if current is None:
            return []
        ids = set(device_ids)
        removed = [did for did in current.island_keys if did in ids]
        current.island_keys = {k: v for k, v in current.island_keys.items() if k not in ids}
        scalar = current.island_key
        clear_scalar = bool(scalar) and (scalar == key or pairing_island_device_id(scalar) in ids)
        if clear_scalar:
            current.island_key = None
        if removed or clear_scalar:
            await self._write(current)
        return removed

    async def async_revoke_island_key(self, device_id: str) -> bool:
        """Remove one device's island access.  Returns True iff something was
        removed.

        Idempotent by design: revoking an id that is already gone returns False
        rather than raising, so a double-tap or a retry is never an error.
        `LEGACY_ISLAND_DEVICE_ID` clears the pre-0.16.0 scalar instead.
        """
        current = await self.load()
        if current is None:
            return False
        if device_id == LEGACY_ISLAND_DEVICE_ID:
            if not current.island_key:
                return False
            current.island_key = None
            await self._write(current)
            return True
        if device_id not in current.island_keys:
            return False
        remaining = {k: v for k, v in current.island_keys.items() if k != device_id}
        current.island_keys = remaining
        await self._write(current)
        return True

    async def async_list_island_devices(self) -> list[dict[str, Any]]:
        """The roster, WITHOUT keys.  The legacy row appears only when the
        pre-0.16.0 scalar is actually set."""
        current = await self.load()
        if current is None:
            return []
        devices = [
            {
                "deviceId": device_id,
                "label": entry.get("label"),
                "pairedAt": entry.get("pairedAt"),
                "isLegacy": False,
            }
            for device_id, entry in current.island_keys.items()
        ]
        if current.island_key:
            devices.append(
                {
                    "deviceId": LEGACY_ISLAND_DEVICE_ID,
                    "label": None,
                    "pairedAt": None,
                    "isLegacy": True,
                }
            )
        return devices

    async def async_get_island_keys(self) -> list[str]:
        """Every island key that should be accepted, newest scheme first."""
        current = await self.load()
        if current is None:
            return []
        return current.all_island_keys()

    async def async_link_signing_key(self, key_id: str, device_id: str) -> None:
        """Record that the phone holding roster row `device_id` signs with
        `key_id`, so revoking the key can take the phone's island key too."""
        current = await self.load()
        if current is None:
            return
        if current.key_devices.get(key_id) == device_id:
            return
        current.key_devices = {**current.key_devices, key_id: device_id}
        await self._write(current)

    async def async_revoke_signing_key(self, key_id: str) -> dict[str, Any]:
        """Untrust `key_id` and remove the island key of the phone it belongs to.

        One write, so a crash cannot leave the phone half-revoked. Raises
        LastAdminKey, and changes nothing, when `key_id` is the only trusted
        key: nothing could sign a command or a calendar write afterwards.
        Idempotent: an unknown key id changes nothing and is not an error.

        Returns `{"untrusted": bool, "deviceId": str | None}`; `deviceId` is
        the roster row removed with it.
        """
        current = await self.load()
        if current is None:
            return {"untrusted": False, "deviceId": None}
        trusted = dict(current.trusted_public_keys_hex)
        if key_id in trusted and len(trusted) == 1:
            raise LastAdminKey(key_id)
        untrusted = trusted.pop(key_id, None) is not None
        device_id = current.key_devices.get(key_id)
        removed_device = device_id if device_id in current.island_keys else None
        newly_revoked = key_id not in current.revoked_key_ids
        if not untrusted and device_id is None and not newly_revoked:
            return {"untrusted": False, "deviceId": None}
        # Recorded even for a key not trusted here: the cloud may still list
        # it, and the next resync would otherwise trust it.
        if newly_revoked:
            current.revoked_key_ids = [*current.revoked_key_ids, key_id]
        current.trusted_public_keys_hex = trusted
        current.trusted_key_ids = sorted(trusted)
        if removed_device is not None:
            current.island_keys = {
                k: v for k, v in current.island_keys.items() if k != removed_device
            }
        current.key_devices = {k: v for k, v in current.key_devices.items() if k != key_id}
        await self._write(current)
        return {"untrusted": untrusted, "deviceId": removed_device}

    async def async_untrust_key(self, key_id: str) -> bool:
        """Untrust `key_id` and record it as revoked, in one write.

        For `DELETE /api/svitgrid/trust-key/{id}`, which removes a key without
        the last-admin check or the phone's island key. Returns True iff the
        key was trusted. A no-op on an empty keystore.
        """
        current = await self.load()
        if current is None:
            return False
        untrusted = key_id in current.trusted_public_keys_hex
        if not untrusted and key_id in current.revoked_key_ids:
            return False
        current.trusted_public_keys_hex = {
            k: v for k, v in current.trusted_public_keys_hex.items() if k != key_id
        }
        current.trusted_key_ids = sorted(current.trusted_public_keys_hex)
        if key_id not in current.revoked_key_ids:
            current.revoked_key_ids = [*current.revoked_key_ids, key_id]
        await self._write(current)
        return untrusted

    async def async_trust_key(self, key_id: str, public_key_hex: str) -> None:
        """Trust `key_id`, and clear it from the revoked ids, in one write.

        The deliberate step that trusts a revoked key again: the owner
        approving a LAN pairing in Home Assistant. A no-op on an empty
        keystore. While edge boxes are adopted, the key is recorded as pending
        on each of them (`box_pending_keys`); with none, nothing is recorded.
        """
        current = await self.load()
        if current is None:
            return
        current.revoked_key_ids = [k for k in current.revoked_key_ids if k != key_id]
        current.trusted_public_keys_hex = {
            **current.trusted_public_keys_hex,
            key_id: public_key_hex,
        }
        current.trusted_key_ids = sorted(current.trusted_public_keys_hex)
        if current.edge_boxes:
            current.box_pending_keys = {
                **current.box_pending_keys,
                key_id: list(current.edge_boxes),
            }
        await self._write(current)

    async def async_clear_box_pending(self, key_id: str, device_id: str) -> None:
        """Record that box `device_id` holds `key_id`: it acked the key's
        `add_trusted_key` with success. The key leaves `box_pending_keys` when
        no box is left waiting for it."""
        current = await self.load()
        if current is None or device_id not in current.box_pending_keys.get(key_id, []):
            return
        waiting = [d for d in current.box_pending_keys[key_id] if d != device_id]
        pending = {k: v for k, v in current.box_pending_keys.items() if k != key_id}
        if waiting:
            pending[key_id] = waiting
        current.box_pending_keys = pending
        await self._write(current)

    async def async_list_pending_box_keys(self) -> list[dict[str, Any]]:
        """The keys trusted here that an adopted box does not hold yet:
        `[{keyId, label, publicKeyHex}]`. `label` names the phone the key is
        linked to, or is None. A key no longer trusted, or pending only on a
        box since forgotten, is not listed."""
        current = await self.load()
        if current is None:
            return []
        listed = []
        for key_id, device_ids in current.box_pending_keys.items():
            public_key_hex = current.trusted_public_keys_hex.get(key_id)
            if public_key_hex is None or not set(device_ids) & set(current.edge_boxes):
                continue
            phone = current.island_keys.get(current.key_devices.get(key_id) or "")
            listed.append(
                {
                    "keyId": key_id,
                    "label": phone.get("label") if isinstance(phone, dict) else None,
                    "publicKeyHex": public_key_hex,
                }
            )
        return listed

    async def async_untrust_entry_keys(self, key_ids: set[str], *, keep_last: bool) -> list[str]:
        """Untrust `key_ids` and drop their phone links, in one write.

        For a config entry being removed. The ids are not recorded as revoked:
        nobody revoked them, and the cloud may rightly trust them for another
        entry. With `keep_last`, nothing changes when the removal would leave no
        trusted key, because another entry still needs one to sign anything.
        Returns the ids that were untrusted.
        """
        current = await self.load()
        if current is None:
            return []
        trusted = {k: v for k, v in current.trusted_public_keys_hex.items() if k not in key_ids}
        removed = sorted(set(current.trusted_public_keys_hex) - set(trusted))
        if keep_last and not trusted and current.trusted_public_keys_hex:
            _LOGGER.warning(
                "Keeping %d trusted key(s) of a removed entry: another entry needs a "
                "trusted admin key",
                len(removed),
            )
            return []
        links = {k: v for k, v in current.key_devices.items() if k not in key_ids}
        if not removed and links == current.key_devices:
            return []
        current.trusted_public_keys_hex = trusted
        current.trusted_key_ids = sorted(trusted)
        current.key_devices = links
        await self._write(current)
        return removed


class LastAdminKey(Exception):
    """Revoking this signing key would leave no trusted admin key."""
