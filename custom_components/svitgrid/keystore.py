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

from .const import STORAGE_KEY, STORAGE_VERSION


def generate_island_key() -> str:
    """Return a new random URL-safe island API key (≥32 chars)."""
    return secrets.token_urlsafe(32)


@dataclass
class KeystoreState:
    api_key: str
    public_key_hex: str
    private_key_pem: str
    signing_key_id: str
    trusted_key_ids: list[str]
    trusted_public_keys_hex: dict[str, str]
    island_key: str | None = None
    # deviceId -> island key.  One entry per paired app instance.  Replaces the
    # single-slot `island_key`, which is retained read-only so a box upgraded
    # from the old scheme keeps its existing device authorized.
    island_keys: dict[str, str] = field(default_factory=dict)

    def all_island_keys(self) -> list[str]:
        """Every currently-valid island key: the per-device map plus the
        legacy scalar.  Order is stable (map first, insertion order), and
        duplicates are collapsed so re-pairing the same device is a no-op."""
        keys: list[str] = []
        for value in self.island_keys.values():
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
            island_keys=dict(data.get("island_keys", {})),
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
        island_keys: dict[str, str] | None = None,
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

    async def async_add_island_key(self, device_id: str, key: str) -> None:
        """Authorize `key` for `device_id`, leaving every other device's key
        intact.

        This is the multi-device replacement for `async_set_island_key`.  The
        old method overwrote the single slot, which silently revoked whichever
        device had paired previously — the app then 401s forever with no way to
        recover except re-pairing, which in turn revoked the other device.
        """
        current = await self.load()
        if current is None:
            return
        current.island_keys = {**current.island_keys, device_id: key}
        await self._store.async_save(asdict(current))

    async def async_get_island_keys(self) -> list[str]:
        """Every island key that should be accepted, newest scheme first."""
        current = await self.load()
        if current is None:
            return []
        return current.all_island_keys()
