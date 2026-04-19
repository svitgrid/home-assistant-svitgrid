"""First-run bootstrap: generate keypair, register public key with the Svitgrid
cloud via POST /edge-devices/bootstrap, persist the returned API key + trusted
keys alongside our keypair via the keystore."""

from __future__ import annotations

import logging

from cryptography.hazmat.primitives import serialization

from .api_client import SvitgridApiClient
from .keystore import KeystoreState, SvitgridKeystore
from .signing import generate_keypair

_LOGGER = logging.getLogger(__name__)


async def run_first_time(
    *,
    api_client: SvitgridApiClient,
    keystore: SvitgridKeystore,
    device_id: str,
    signing_key_id: str,
) -> KeystoreState:
    """Generate keypair, call bootstrap, save state. Raises the api_client
    exception if bootstrap fails — caller decides whether to surface to the
    user or retry."""
    private_key, public_key_hex = generate_keypair()
    _LOGGER.info(
        "Bootstrapping Svitgrid integration: deviceId=%s, signingKeyId=%s",
        device_id,
        signing_key_id,
    )
    resp = await api_client.bootstrap(
        device_id=device_id,
        public_key_hex=public_key_hex,
        signing_key_id=signing_key_id,
    )

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    await keystore.save(
        api_key=resp["apiKey"],
        public_key_hex=public_key_hex,
        private_key_pem=pem,
        signing_key_id=signing_key_id,
        trusted_key_ids=list(resp.get("trustedKeyIds", [])),
    )
    state = await keystore.load()
    assert state is not None
    _LOGGER.info(
        "Svitgrid bootstrap complete: apiKey=%s..., trustedKeys=%s",
        state.api_key[:8],
        state.trusted_key_ids,
    )
    return state
