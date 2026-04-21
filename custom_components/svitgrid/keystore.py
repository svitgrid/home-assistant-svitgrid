"""Wraps HA's Store for the add-on's persistent state: API key, keypair,
signingKeyId, and the cached trustedKeyIds list."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION


@dataclass
class KeystoreState:
    api_key: str
    public_key_hex: str
    private_key_pem: str
    signing_key_id: str
    trusted_key_ids: list[str]
    trusted_public_keys_hex: dict[str, str]

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
        "trusted_key_ids": ["keyId1", "keyId2"]
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
    ) -> None:
        await self._store.async_save(
            asdict(
                KeystoreState(
                    api_key=api_key,
                    public_key_hex=public_key_hex,
                    private_key_pem=private_key_pem,
                    signing_key_id=signing_key_id,
                    trusted_key_ids=trusted_key_ids,
                    trusted_public_keys_hex=trusted_public_keys_hex or {},
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
