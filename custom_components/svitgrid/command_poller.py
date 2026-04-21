"""Command poller loop: every 5s, GETs /executors/commands, and for each
command either (1) handles it internally (trust-cache management), (2)
dispatches it to the configured executor after verifying the admin
signature, or (3) ACKs it as unsupported.

Replaces B1's blanket "reject everything" behavior. Also removes B1's
empty-cache fallback — signed commands against an empty cache are now
skipped (no ACK) so forged commands can't be ACKed as if they were valid."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant

from .api_client import CommandAckFailed, SvitgridApiClient
from .const import (
    ADD_TRUSTED_KEY_COMMAND,
    COMMAND_POLL_INTERVAL_S,
    DISPATCHABLE_COMMANDS,
    REVOKE_TRUSTED_KEY_COMMAND,
)
from .keystore import SvitgridKeystore
from .signing import sign_payload, verify_payload

if TYPE_CHECKING:
    from .executors.base import BaseExecutor

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
    keystore: SvitgridKeystore,
    executor: BaseExecutor | None,
) -> None:
    """Process one polled command. Three dispatch arms:
      1. Internal trust commands (add_trusted_key, revoke_trusted_key) —
         update cache + keystore, ACK success.
      2. Dispatchable inverter commands — verify admin signature, dispatch
         to executor, ACK with executor result.
      3. Everything else — ACK with reason='unsupported'.

    Unlike B1, there is no empty-cache fallback. Signed commands against
    an empty cache are skipped (no ACK) — that's the correct security
    behavior for a freshly-installed add-on in a household whose admin
    keys haven't propagated yet (re-bootstrap to fetch them)."""
    cmd_id = command.get("commandId")
    if not cmd_id:
        _LOGGER.warning("Skipping command with no commandId: %s", command)
        return
    cmd_type = command.get("command")

    # === Arm 1: Internal trust commands ===
    if cmd_type == ADD_TRUSTED_KEY_COMMAND:
        payload = command.get("payload") or {}
        key_id = payload.get("signingKeyId")
        public_key_hex = payload.get("publicKeyHex")
        if not key_id or not public_key_hex:
            _LOGGER.warning("Skipping malformed add_trusted_key %s: %s", cmd_id, payload)
            return
        trusted_public_keys_hex[key_id] = public_key_hex
        await keystore.update_trusted_keys_hex(dict(trusted_public_keys_hex))
        _LOGGER.info(
            "Added trusted key %s to cache (now %d keys)",
            key_id,
            len(trusted_public_keys_hex),
        )
        await _send_signed_ack(
            api_client=api_client,
            api_key=api_key,
            command_id=cmd_id,
            success=True,
            result={"addedKeyId": key_id},
            our_private_key=our_private_key,
            our_signing_key_id=our_signing_key_id,
            executor_version=executor_version,
        )
        return

    if cmd_type == REVOKE_TRUSTED_KEY_COMMAND:
        payload = command.get("payload") or {}
        key_id = payload.get("signingKeyId")
        if not key_id:
            _LOGGER.warning("Skipping malformed revoke_trusted_key %s: %s", cmd_id, payload)
            return
        trusted_public_keys_hex.pop(key_id, None)
        await keystore.update_trusted_keys_hex(dict(trusted_public_keys_hex))
        _LOGGER.info(
            "Revoked trusted key %s (now %d keys)",
            key_id,
            len(trusted_public_keys_hex),
        )
        await _send_signed_ack(
            api_client=api_client,
            api_key=api_key,
            command_id=cmd_id,
            success=True,
            result={"revokedKeyId": key_id},
            our_private_key=our_private_key,
            our_signing_key_id=our_signing_key_id,
            executor_version=executor_version,
        )
        return

    # === Arms 2 and 3 require a verified admin signature ===
    sig = command.get("signature")
    sig_key_id = command.get("signingKeyId")
    signed_event_data = command.get("signedEventData")

    if not sig or not sig_key_id or signed_event_data is None:
        _LOGGER.warning("Skipping unsigned non-internal command %s", cmd_id)
        return

    admin_pub_hex = trusted_public_keys_hex.get(sig_key_id)
    if not admin_pub_hex:
        _LOGGER.warning(
            "Skipping command %s — signingKeyId %s not in trusted keys (cache has %d)",
            cmd_id,
            sig_key_id,
            len(trusted_public_keys_hex),
        )
        return

    if not verify_payload(signed_event_data, sig, admin_pub_hex):
        _LOGGER.warning("Skipping command %s — admin signature verification failed", cmd_id)
        return

    # === Arm 2: Dispatchable commands ===
    if cmd_type in DISPATCHABLE_COMMANDS:
        if executor is None:
            _LOGGER.warning(
                "Command %s (%s) dispatchable but no executor configured; rejecting as unsupported",
                cmd_id,
                cmd_type,
            )
            await _send_signed_ack(
                api_client=api_client,
                api_key=api_key,
                command_id=cmd_id,
                success=False,
                rejected=True,
                reason="no_executor_configured",
                our_private_key=our_private_key,
                our_signing_key_id=our_signing_key_id,
                executor_version=executor_version,
            )
            return

        try:
            result = await executor.set_battery_charge(command.get("payload") or {})
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Executor failed on command %s", cmd_id)
            await _send_signed_ack(
                api_client=api_client,
                api_key=api_key,
                command_id=cmd_id,
                success=False,
                rejected=False,
                reason=f"executor_error: {err}",
                our_private_key=our_private_key,
                our_signing_key_id=our_signing_key_id,
                executor_version=executor_version,
            )
            return
        await _send_signed_ack(
            api_client=api_client,
            api_key=api_key,
            command_id=cmd_id,
            success=True,
            result=result,
            our_private_key=our_private_key,
            our_signing_key_id=our_signing_key_id,
            executor_version=executor_version,
        )
        return

    # === Arm 3: Unrecognized command — reject as unsupported ===
    await _send_signed_ack(
        api_client=api_client,
        api_key=api_key,
        command_id=cmd_id,
        success=False,
        rejected=True,
        reason="unsupported",
        our_private_key=our_private_key,
        our_signing_key_id=our_signing_key_id,
        executor_version=executor_version,
    )


async def _send_signed_ack(
    *,
    api_client: SvitgridApiClient,
    api_key: str,
    command_id: str,
    success: bool,
    our_private_key: ec.EllipticCurvePrivateKey,
    our_signing_key_id: str,
    executor_version: str,
    result: dict | None = None,
    rejected: bool | None = None,
    reason: str | None = None,
) -> None:
    """Build + sign an ACK. commandId is FIRST in the signed payload
    (Plan A's enforceHaSignedAck requires it for replay protection)."""
    ack_fields: dict[str, Any] = {
        "success": success,
        "executorTime": _now_iso(),
        "executorVersion": executor_version,
    }
    if result is not None:
        ack_fields["result"] = result
    if rejected is not None:
        ack_fields["rejected"] = rejected
    if reason is not None:
        ack_fields["reason"] = reason

    signed_payload = {"commandId": command_id, **ack_fields}
    signature = sign_payload(signed_payload, our_private_key)

    try:
        await api_client.ack_command(
            api_key=api_key,
            command_id=command_id,
            body={
                **ack_fields,
                "signature": signature,
                "signingKeyId": our_signing_key_id,
            },
        )
    except CommandAckFailed:
        _LOGGER.exception("ACK for command %s rejected by server", command_id)


async def run_loop(
    *,
    hass: HomeAssistant,
    api_client: SvitgridApiClient,
    keystore: SvitgridKeystore,
    trusted_public_keys_hex: dict[str, str],
    executor_version: str,
    executor: BaseExecutor | None,
    interval_s: int = COMMAND_POLL_INTERVAL_S,
) -> None:
    """Polling coroutine. Exits when hass.is_stopping becomes True.

    `trusted_public_keys_hex` is a dict signingKeyId → publicKeyHex. Initially
    populated from the bootstrap response; mutated live when add_trusted_key /
    revoke_trusted_key commands arrive (persisted via keystore)."""
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
                    keystore=keystore,
                    executor=executor,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Command poll iteration failed; retrying next tick")
        await asyncio.sleep(interval_s)
    _LOGGER.info("Command poller stopped")
