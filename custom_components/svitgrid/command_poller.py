"""Command poller loop: GETs /executors/commands on a server-driven cadence (pollIntervalMs, 5s–10min), short-circuited by the MQTT wake-bell. For each
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

from homeassistant.config_entries import ConfigEntry

from .api_client import CommandAckFailed, DeviceEvicted, DeviceStopped, SvitgridApiClient
from .cloud_endpoint_handler import is_allowed_api_base
from .const import (
    ADD_TRUSTED_KEY_COMMAND,
    COMMAND_POLL_CEILING_S,
    COMMAND_POLL_INTERVAL_S,
    DISPATCHABLE_COMMANDS,
    REVOKE_TRUSTED_KEY_COMMAND,
    SET_CLOUD_ENDPOINT_COMMAND,
)
from .keystore import SvitgridKeystore
from .signing import sign_payload, verify_payload

if TYPE_CHECKING:
    from .executors.base import BaseExecutor


def apply_cloud_endpoint_change(hass, entry, url):
    """Module-level shim for `apply_cloud_endpoint_change` — exists to solve
    TWO constraints simultaneously:

    1. CIRCULAR IMPORT: __init__.py imports from command_poller during entry
       setup (`from .command_poller import run_loop`), so command_poller cannot
       do `from . import apply_cloud_endpoint_change` at module load time.

    2. TEST PATCH TARGET: tests patch
       `custom_components.svitgrid.command_poller.apply_cloud_endpoint_change`.
       For the patch to intercept the call inside process_command, the name
       must resolve in THIS module's namespace, not __init__.py's. A bare
       deferred import inside process_command would resolve fresh each call
       and bypass the patch.

    The shim sits at module scope (so patching works) and uses a deferred
    import in its body (so circular import doesn't fire at load time).
    In tests, patch() replaces this function before process_command runs —
    the shim body never executes."""
    from . import apply_cloud_endpoint_change as _real  # noqa: PLC0415

    _real(hass, entry, url)

_LOGGER = logging.getLogger(__name__)


def _next_poll_interval_s(response: dict | None, floor_s: float) -> float:
    """Pick the next command-poll sleep from the server's pollIntervalMs.

    Mirrors readings_publisher._next_interval_s. The server returns
    pollIntervalMs on every 2xx poll (5_000 when a command is pending,
    up to 600_000 when idle). We clamp to [floor_s, COMMAND_POLL_CEILING_S]:
    the floor is the user-configured command_poll_interval_seconds (so a
    fast cadence is honored but never tighter than the user asked), and the
    ceiling caps how long a misbehaving server can park us. When the field
    is missing (old server, or the error-path empty response), fall back to
    the floor — preserving the legacy fixed-cadence behavior."""
    raw_ms = response.get("pollIntervalMs") if response else None
    if not isinstance(raw_ms, (int, float)):
        return float(floor_s)
    seconds = raw_ms / 1000.0
    if seconds < floor_s:
        return float(floor_s)
    if seconds > COMMAND_POLL_CEILING_S:
        return float(COMMAND_POLL_CEILING_S)
    return float(seconds)


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
    keystore: SvitgridKeystore | None,
    executors_by_inverter: dict[str, "BaseExecutor"] | None = None,
    hass: HomeAssistant | None = None,
    entry: "ConfigEntry | None" = None,
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
        if keystore is not None:
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
        if keystore is not None:
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

    # === Arm 1c: set_cloud_endpoint (sub-project E) ===
    # Internal because the URL allow-list IS the trust boundary; an admin
    # signature would add nothing — only Svitgrid controls the two URLs
    # that pass `is_allowed_api_base`.
    if cmd_type == SET_CLOUD_ENDPOINT_COMMAND:
        payload = command.get("payload") or {}
        url = payload.get("url")
        if not is_allowed_api_base(url):
            _LOGGER.warning(
                "set_cloud_endpoint rejected — url not in allow-list: %r", url,
            )
            await _send_signed_ack(
                api_client=api_client,
                api_key=api_key,
                command_id=cmd_id,
                success=False,
                rejected=True,
                reason="disallowed_url",
                our_private_key=our_private_key,
                our_signing_key_id=our_signing_key_id,
                executor_version=executor_version,
            )
            return

        # YAML-config installs don't have a ConfigEntry to mutate — ACK
        # rejected (NOT success) so the cloud audit log accurately reflects
        # that this install can't auto-migrate. Operator falls back to
        # instructing the user to update the YAML config manually.
        if hass is None or entry is None:
            _LOGGER.warning(
                "set_cloud_endpoint rejected — no ConfigEntry (YAML install?). "
                "cmd_id=%s url=%s", cmd_id, url,
            )
            await _send_signed_ack(
                api_client=api_client,
                api_key=api_key,
                command_id=cmd_id,
                success=False,
                rejected=True,
                reason="yaml_config_no_entry",
                our_private_key=our_private_key,
                our_signing_key_id=our_signing_key_id,
                executor_version=executor_version,
            )
            return

        # ACK SUCCESS FIRST on the ORIGINAL endpoint, THEN apply the
        # change. If apply came first, the reload would tear down the
        # api_client mid-flight and the ACK would never reach the cloud,
        # leaving cmd.status stuck in `delivered` forever (the firmware
        # sub-project D5 ack-restart-race, same root cause).
        await _send_signed_ack(
            api_client=api_client,
            api_key=api_key,
            command_id=cmd_id,
            success=True,
            result={"appliedUrl": url},
            our_private_key=our_private_key,
            our_signing_key_id=our_signing_key_id,
            executor_version=executor_version,
        )

        try:
            apply_cloud_endpoint_change(hass, entry, url)
        except Exception:
            # ACK already sent as success — the cloud audit log will say
            # the migration succeeded, but the local apply just failed.
            # Operator must reconcile manually. Distinctive log message
            # for grep-based recovery: "set_cloud_endpoint apply failed".
            _LOGGER.exception(
                "set_cloud_endpoint apply failed AFTER successful ACK — "
                "integration still on old endpoint, cloud thinks migration "
                "done. cmd_id=%s url=%s manual recovery required.",
                cmd_id, url,
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
        payload = command.get("payload") or {}
        inverter_id = payload.get("inverterId")
        # None inverter_id (missing from payload) also falls through to the
        # no-executor rejection below — .get(None) misses on a str-keyed dict.
        executor = (executors_by_inverter or {}).get(inverter_id)
        if executor is None:
            _LOGGER.warning(
                "Command %s (%s) for inverter %s has no executor; rejecting",
                cmd_id,
                cmd_type,
                inverter_id,
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
            # P2A A5: generic dispatch instead of hardcoded
            # set_battery_charge. BaseExecutor.dispatch routes unknown
            # command names to NotImplementedError so SmgIiExecutor (which
            # only knows set_battery_charge) keeps working unchanged;
            # YamlDispatcher overrides dispatch to handle all 4 commands.
            result = await executor.dispatch(cmd_type, payload)
        except NotImplementedError as err:
            _LOGGER.info(
                "Executor doesn't support %s — ACKing as unsupported", cmd_type,
            )
            await _send_signed_ack(
                api_client=api_client,
                api_key=api_key,
                command_id=cmd_id,
                success=False,
                rejected=True,
                reason=f"unsupported: {err}",
                our_private_key=our_private_key,
                our_signing_key_id=our_signing_key_id,
                executor_version=executor_version,
            )
            return
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
    keystore: SvitgridKeystore | None,
    trusted_public_keys_hex: dict[str, str] | None = None,
    executor_version: str = "0.2.0",
    executors_by_inverter: dict[str, "BaseExecutor"] | None = None,
    interval_s: int = COMMAND_POLL_INTERVAL_S,
    entry_data: dict | None = None,
    wake_event: asyncio.Event | None = None,
    activity: Any = None,  # ActivityTracker; None acceptable
    lifecycle: Any = None,  # LifecycleState; None acceptable (keeps existing callers working)
    store: Any = None,  # store with async set_lifecycle; None acceptable
    entry: "ConfigEntry | None" = None,
) -> None:
    """Polling coroutine. Exits when hass.is_stopping becomes True.

    `trusted_public_keys_hex` is a dict signingKeyId → publicKeyHex. Initially
    populated from the bootstrap response; mutated live when add_trusted_key /
    revoke_trusted_key commands arrive (persisted via keystore).

    When called from the config-entry path, `keystore` is None and `entry_data`
    carries the key material from ConfigEntry.data. In that case a transient
    KeystoreState is built on each iteration from entry_data (no persistence
    write-back; trust mutations are not persisted in Phase 1)."""
    _LOGGER.info("Command poller started (interval=%ss)", interval_s)
    # Mutable cache — shared across iterations so add/revoke live-mutations work.
    # In the config-entry path (keystore=None) we seed from entry_data once.
    if trusted_public_keys_hex is None:
        trusted_public_keys_hex = {}
        if entry_data:
            for item in entry_data.get("trusted_keys", []):
                if isinstance(item, dict):
                    kid = item.get("signingKeyId") or item.get("key_id")
                    pub = item.get("publicKeyHex") or item.get("public_key_hex")
                    if kid and pub:
                        trusted_public_keys_hex[kid] = pub

    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        next_interval_s = float(interval_s)  # floor; updated from each poll response
        try:
            if keystore is not None:
                state = await keystore.load()
                if state is None:
                    _LOGGER.error("Command poller: keystore empty; stopping loop")
                    return
            else:
                if not entry_data:
                    _LOGGER.error("Command poller: no keystore and no entry_data; stopping loop")
                    return
                from .keystore import KeystoreState  # local import avoids circular dep

                state = KeystoreState(
                    api_key=entry_data["api_key"],
                    public_key_hex=entry_data["public_key_hex"],
                    private_key_pem=entry_data["private_key_pem"],
                    signing_key_id=entry_data["signing_key_id"],
                    trusted_key_ids=list(trusted_public_keys_hex.keys()),
                    trusted_public_keys_hex=dict(trusted_public_keys_hex),
                )
            resp = await api_client.poll_commands(api_key=state.api_key)
            next_interval_s = _next_poll_interval_s(resp, interval_s)
            for command in resp.get("commands", []):
                if activity is not None:
                    activity.record_command(
                        kind=str(command.get("command") or "unknown"),
                        payload=command.get("payload") or {},
                        result=None,
                        success=True,
                    )
                await process_command(
                    command=command,
                    api_client=api_client,
                    api_key=state.api_key,
                    trusted_public_keys_hex=trusted_public_keys_hex,
                    our_private_key=state.load_private_key(),
                    our_signing_key_id=state.signing_key_id,
                    executor_version=executor_version,
                    keystore=keystore,  # type: ignore[arg-type]
                    executors_by_inverter=executors_by_inverter,
                    hass=hass,
                    entry=entry,
                )
        except DeviceEvicted:
            # 410 Gone — owning household deleted. Authoritative eviction:
            # stop all command polling (matches ESP32 firmware behavior).
            _LOGGER.error(
                "Command poller: device key revoked (410); stopping. "
                "Re-pair the integration to resume."
            )
            if lifecycle is not None:
                lifecycle.deprovision("Device key revoked (owner household removed)", _now_iso())
                if store is not None:
                    await store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since)
            return
        except DeviceStopped as e:
            _LOGGER.warning(
                "Command poller: server signaled stop (%s); stopping loop. "
                "Operator can re-enable the device and you can reload the "
                "integration to resume.",
                e.reason,
            )
            if lifecycle is not None:
                lifecycle.pause(str(e.reason), _now_iso())
                if store is not None:
                    await store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since)
            return
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Command poll iteration failed; retrying next tick")
        # Sleep until the server-driven interval OR an MQTT wake-bell, whichever
        # comes first. next_interval_s reflects the latest poll's pollIntervalMs.
        if wake_event is not None:
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=next_interval_s)
                wake_event.clear()
            except TimeoutError:
                pass  # normal interval elapse — proceed to poll
        else:
            await asyncio.sleep(next_interval_s)
    _LOGGER.info("Command poller stopped")
