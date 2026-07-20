"""Wraps HA's Store for the add-on's persistent state: API key, keypair,
signingKeyId, the cached trustedKeyIds list, and the island API key."""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import LEGACY_ISLAND_DEVICE_ID, STORAGE_KEY, STORAGE_VERSION


def generate_island_key() -> str:
    """Return a new random URL-safe island API key (≥32 chars)."""
    return secrets.token_urlsafe(32)


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
        "island_key": "<url-safe token or null>"
      }
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)

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
        await self._store.async_save(
            asdict(
                KeystoreState(
                    api_key=api_key,
                    public_key_hex=public_key_hex,
                    private_key_pem=private_key_pem,
                    signing_key_id=signing_key_id,
                    trusted_key_ids=trusted_key_ids,
                    trusted_public_keys_hex=trusted_public_keys_hex or {},
                    island_key=resolved_island_key,
                    island_keys=resolved_island_keys or {},
                )
            )
        )

    async def update_trusted_keys(self, trusted_key_ids: list[str]) -> None:
        """Used by command_poller when it receives add_trusted_key / revoke_trusted_key."""
        current = await self.load()
        if current is None:
            return
        current.trusted_key_ids = trusted_key_ids
        await self._store.async_save(asdict(current))

    async def update_trusted_keys_hex(self, trusted_public_keys_hex: dict[str, str]) -> None:
        """Replace the trusted-keys cache atomically. Keeps trusted_key_ids in
        sync (derived as the dict's keys)."""
        current = await self.load()
        if current is None:
            return
        current.trusted_public_keys_hex = dict(trusted_public_keys_hex)
        current.trusted_key_ids = sorted(trusted_public_keys_hex.keys())
        await self._store.async_save(asdict(current))

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
        await self._store.async_save(asdict(current))

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
        await self._store.async_save(asdict(current))

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
            await self._store.async_save(asdict(current))
            return True
        if device_id not in current.island_keys:
            return False
        remaining = {k: v for k, v in current.island_keys.items() if k != device_id}
        current.island_keys = remaining
        await self._store.async_save(asdict(current))
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
                {"deviceId": LEGACY_ISLAND_DEVICE_ID, "label": None, "pairedAt": None, "isLegacy": True}
            )
        return devices

    async def async_get_island_keys(self) -> list[str]:
        """Every island key that should be accepted, newest scheme first."""
        current = await self.load()
        if current is None:
            return []
        return current.all_island_keys()
