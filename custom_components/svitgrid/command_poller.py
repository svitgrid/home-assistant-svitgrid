"""Command poller loop: every 5s, GETs /executors/commands, for each
command verifies the admin signature against the local trusted-keys
cache, and ACKs with a signed rejection (B1: every command is unsupported).

B2 will extend process_command to dispatch recognized commands to
executors (SMG-II first)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant

from .api_client import CommandAckFailed, SvitgridApiClient
from .const import COMMAND_POLL_INTERVAL_S
from .keystore import SvitgridKeystore
from .signing import sign_payload, verify_payload

_LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def process_command(
    *,
    command: dict[str, Any],
    api_client: SvitgridApiClient,
    api_key: str,
    trusted_public_keys_hex: dict[str, str],
    our_private_key: ec.EllipticCurvePrivateKey,
    our_signing_key_id: str,
    executor_version: str,
) -> None:
    """Verify one polled command's admin signature, then send a signed
    rejection ACK (B1 behaviour: reject everything with reason 'unsupported')."""
    cmd_id = command.get("commandId")
    if not cmd_id:
        _LOGGER.warning("Skipping command with no commandId: %s", command)
        return

    sig = command.get("signature")
    sig_key_id = command.get("signingKeyId")
    signed_event_data = command.get("signedEventData")

    if not sig or not sig_key_id or signed_event_data is None:
        _LOGGER.warning("Skipping unsigned command %s", cmd_id)
        return

    # B1 degradation: when the trusted-keys cache is empty, we can't verify
    # admin signatures (Plan A's bootstrap response doesn't include hex per
    # keyId). We still send the signed rejection ACK so the wire-protocol
    # contract is validated end-to-end against staging. B2 will populate
    # this cache by processing add_trusted_key commands and this fallback
    # will stop triggering.
    if not trusted_public_keys_hex:
        _LOGGER.warning(
            "B1 bootstrap mode: trusted-keys cache empty; sending signed ACK "
            "for command %s WITHOUT verifying admin signature. This stops "
            "happening once the add-on receives add_trusted_key commands.",
            cmd_id,
        )
    else:
        admin_pub_hex = trusted_public_keys_hex.get(sig_key_id)
        if not admin_pub_hex:
            _LOGGER.warning(
                "Skipping command %s — signingKeyId %s not in trusted keys",
                cmd_id,
                sig_key_id,
            )
            return

        if not verify_payload(signed_event_data, sig, admin_pub_hex):
            _LOGGER.warning("Skipping command %s — signature verification failed", cmd_id)
            return

    # Build + sign rejection ACK. commandId is FIRST in the signed payload
    # (Plan A's enforceHaSignedAck requires it for replay protection).
    ack_fields = {
        "success": False,
        "rejected": True,
        "reason": "unsupported",
        "executorTime": _now_iso(),
        "executorVersion": executor_version,
    }
    signed_payload = {"commandId": cmd_id, **ack_fields}
    signature = sign_payload(signed_payload, our_private_key)

    try:
        await api_client.ack_command(
            api_key=api_key,
            command_id=cmd_id,
            body={
                **ack_fields,
                "signature": signature,
                "signingKeyId": our_signing_key_id,
            },
        )
    except CommandAckFailed:
        _LOGGER.exception("ACK for command %s rejected by server", cmd_id)


async def run_loop(
    *,
    hass: HomeAssistant,
    api_client: SvitgridApiClient,
    keystore: SvitgridKeystore,
    trusted_public_keys_hex: dict[str, str],
    executor_version: str,
    interval_s: int = COMMAND_POLL_INTERVAL_S,
) -> None:
    """Polling coroutine. Exits when hass.is_stopping becomes True.

    `trusted_public_keys_hex` is a dict signingKeyId → publicKeyHex; in B1
    it's seeded at bootstrap time. B2 will listen for add_trusted_key /
    revoke_trusted_key commands and mutate this dict in-place."""
    _LOGGER.info("Command poller started (interval=%ss)", interval_s)
    while not hass.is_stopping:
        try:
            state = await keystore.load()
            if state is None:
                _LOGGER.error("Command poller: keystore empty; stopping loop")
                return
            resp = await api_client.poll_commands(api_key=state.api_key)
            for command in resp.get("commands", []):
                await process_command(
                    command=command,
                    api_client=api_client,
                    api_key=state.api_key,
                    trusted_public_keys_hex=trusted_public_keys_hex,
                    our_private_key=state.load_private_key(),
                    our_signing_key_id=state.signing_key_id,
                    executor_version=executor_version,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Command poll iteration failed; retrying next tick")
        await asyncio.sleep(interval_s)
    _LOGGER.info("Command poller stopped")
