"""Command poller processes one polled command: verifies its signature
against the local trusted-keys cache, then signs + POSTs a rejection ACK."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization

from custom_components.svitgrid.api_client import DeviceEvicted, DeviceStopped
from custom_components.svitgrid.command_poller import (
    _next_poll_interval_s,
    process_command,
)
from custom_components.svitgrid.command_poller import (
    run_loop as poller_run_loop,
)
from custom_components.svitgrid.const import COMMAND_POLL_INTERVAL_S
from custom_components.svitgrid.keystore import KeystoreState
from custom_components.svitgrid.signing import (
    generate_keypair,
    sign_payload,
    verify_payload,
)


def _make_keystore_state(priv, pub_hex, trusted_hex_by_id):
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return KeystoreState(
        api_key="our-api-key",
        public_key_hex=pub_hex,
        private_key_pem=pem,
        signing_key_id="our-key",
        trusted_key_ids=list(trusted_hex_by_id.keys()),
        trusted_public_keys_hex=dict(trusted_hex_by_id),
    )


@pytest.mark.asyncio
async def test_rejects_with_signed_ack_when_signature_valid():
    """Admin key known + valid signature on a non-dispatchable command →
    send signed rejection ACK with reason='unsupported' (Arm 3)."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-1", "command": "some_future_command"}
    admin_sig = sign_payload(signed_event_data, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "some_future_command",
        "payload": {},
        "signature": admin_sig,
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }

    trusted_by_id = {"admin-key-id": admin_pub_hex}
    state = _make_keystore_state(our_priv, our_pub_hex, trusted_by_id)

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key=state.api_key,
        trusted_public_keys_hex=trusted_by_id,
        our_private_key=our_priv,
        our_signing_key_id=state.signing_key_id,
        executor_version="0.1.0",
        keystore=AsyncMock(),
        executors_by_inverter=None,
    )

    api_client.ack_command.assert_called_once()
    kwargs = api_client.ack_command.call_args.kwargs
    assert kwargs["command_id"] == "cmd-1"
    ack_body = kwargs["body"]
    assert ack_body["rejected"] is True
    assert ack_body["reason"] == "unsupported"
    assert ack_body["success"] is False
    assert ack_body["signingKeyId"] == "our-key"
    assert "signature" in ack_body

    # ACK signature verifies under our OWN public key over commandId + ack fields.
    signed_ack = {
        "commandId": "cmd-1",
        **{k: v for k, v in ack_body.items() if k not in ("signature", "signingKeyId")},
    }
    assert verify_payload(signed_ack, ack_body["signature"], our_pub_hex)


@pytest.mark.asyncio
async def test_skips_command_with_invalid_signature():
    """Admin key known but signature doesn't verify → skip, don't ACK."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, _our_pub_hex = generate_keypair()

    # Signature over DIFFERENT payload — tampered.
    admin_sig = sign_payload({"commandId": "cmd-different"}, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "signature": admin_sig,
        "signingKeyId": "admin-key-id",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="key",
        trusted_public_keys_hex={"admin-key-id": admin_pub_hex},
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.1.0",
        keystore=AsyncMock(),
        executors_by_inverter=None,
    )

    # Invalid signature → skip, don't ACK (server's retry / expiry handles it).
    api_client.ack_command.assert_not_called()


@pytest.mark.asyncio
async def test_skips_command_with_unknown_signing_key_when_cache_is_populated():
    """Cache populated but signingKeyId not in it → skip.
    Distinct from the empty-cache B1-bootstrap-mode fallback below."""
    admin_priv, admin_pub_hex = generate_keypair()
    other_priv, _ = generate_keypair()
    our_priv, _ = generate_keypair()
    admin_sig = sign_payload({"commandId": "cmd-1"}, other_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "signature": admin_sig,
        "signingKeyId": "UNKNOWN-KEY",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="key",
        # Non-empty cache but "UNKNOWN-KEY" isn't in it
        trusted_public_keys_hex={"some-other-known-key": admin_pub_hex},
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.1.0",
        keystore=AsyncMock(),
        executors_by_inverter=None,
    )
    api_client.ack_command.assert_not_called()


@pytest.mark.asyncio
async def test_add_trusted_key_command_updates_cache_and_acks_success():
    """add_trusted_key is an internal command: extract publicKeyHex +
    signingKeyId from payload, update in-memory cache + persist via
    keystore, ACK with success."""
    our_priv, our_pub_hex = generate_keypair()
    trusted_cache: dict[str, str] = {}

    command = {
        "commandId": "add-trust-1",
        "command": "add_trusted_key",
        "payload": {
            "publicKeyHex": "04" + "dd" * 64,
            "signingKeyId": "new-admin-key",
        },
        # Internal commands don't require a signed admin payload
        "signature": None,
        "signingKeyId": None,
        "signedEventData": None,
    }

    api_client = AsyncMock()
    keystore = AsyncMock()
    keystore.update_trusted_keys_hex = AsyncMock()

    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex=trusted_cache,
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.2.0",
        keystore=keystore,
        executors_by_inverter=None,
    )

    # In-memory cache mutated
    assert trusted_cache == {"new-admin-key": "04" + "dd" * 64}
    # Persisted via keystore
    keystore.update_trusted_keys_hex.assert_called_once_with({"new-admin-key": "04" + "dd" * 64})
    # ACK with success
    api_client.ack_command.assert_called_once()
    ack_body = api_client.ack_command.call_args.kwargs["body"]
    assert ack_body["success"] is True


@pytest.mark.asyncio
async def test_revoke_trusted_key_command_removes_from_cache():
    our_priv, _ = generate_keypair()
    trusted_cache = {
        "admin-a": "04" + "aa" * 64,
        "admin-b": "04" + "bb" * 64,
    }

    command = {
        "commandId": "revoke-1",
        "command": "revoke_trusted_key",
        "payload": {"signingKeyId": "admin-b"},
        "signature": None,
        "signingKeyId": None,
        "signedEventData": None,
    }

    api_client = AsyncMock()
    keystore = AsyncMock()
    keystore.update_trusted_keys_hex = AsyncMock()

    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex=trusted_cache,
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.2.0",
        keystore=keystore,
        executors_by_inverter=None,
    )

    assert trusted_cache == {"admin-a": "04" + "aa" * 64}
    keystore.update_trusted_keys_hex.assert_called_once_with({"admin-a": "04" + "aa" * 64})
    ack_body = api_client.ack_command.call_args.kwargs["body"]
    assert ack_body["success"] is True


@pytest.mark.asyncio
async def test_empty_cache_rejects_write_command_no_fallback():
    """B1-fallback is GONE. Empty trusted-keys cache + writable command →
    skip (no ACK), same as unknown signing key."""
    admin_priv, _ = generate_keypair()
    our_priv, _ = generate_keypair()
    admin_sig = sign_payload({"commandId": "cmd-1"}, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "payload": {"chargePowerLimitW": 3000},
        "signature": admin_sig,
        "signingKeyId": "some-admin",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},  # empty cache — no fallback anymore
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.2.0",
        keystore=AsyncMock(),
        executors_by_inverter=None,
    )

    # No ACK sent — command is skipped, let server expiry handle it.
    api_client.ack_command.assert_not_called()


class TestNextPollInterval:
    def test_honors_idle_server_value(self):
        assert _next_poll_interval_s({"pollIntervalMs": 600_000}, floor_s=5) == 600.0

    def test_honors_fast_server_value_down_to_floor(self):
        assert _next_poll_interval_s({"pollIntervalMs": 5_000}, floor_s=5) == 5.0

    def test_defaults_to_floor_when_field_missing(self):
        assert _next_poll_interval_s({"commands": []}, floor_s=5) == 5.0

    def test_defaults_to_floor_when_response_none(self):
        assert _next_poll_interval_s(None, floor_s=5) == 5.0

    def test_clamps_to_ceiling(self):
        assert _next_poll_interval_s({"pollIntervalMs": 999_999_999}, floor_s=5) == 600.0

    def test_clamps_up_to_floor(self):
        assert _next_poll_interval_s({"pollIntervalMs": 200}, floor_s=5) == 5.0
        assert _next_poll_interval_s({"pollIntervalMs": -10}, floor_s=5) == 5.0

    def test_non_numeric_pollintervalms_falls_back_to_floor(self):
        assert _next_poll_interval_s({"pollIntervalMs": "soon"}, floor_s=30) == 30.0


# ---------------------------------------------------------------------------
# run_loop tests (Task 3: server-driven cadence + 410 eviction stop)
# ---------------------------------------------------------------------------


def _hass_one_iter() -> MagicMock:
    hass = MagicMock()
    n = {"i": 0}

    def _is_stopping(_self):
        n["i"] += 1
        return n["i"] > 1

    type(hass).is_stopping = property(_is_stopping)
    return hass


def _entry_data():
    priv, pub_hex = generate_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "api_key": "k",
        "public_key_hex": pub_hex,
        "private_key_pem": pem,
        "signing_key_id": "our-key",
        "trusted_keys": [],
    }


async def _run_poller_capture_sleep(monkeypatch, hass, api, *, interval_s=None):
    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    kwargs = dict(hass=hass, api_client=api, keystore=None, entry_data=_entry_data(), wake_event=None)
    if interval_s is not None:
        kwargs["interval_s"] = interval_s
    await poller_run_loop(**kwargs)
    return sleeps


@pytest.mark.asyncio
async def test_loop_honors_idle_poll_interval(monkeypatch):
    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [], "pollIntervalMs": 600_000})
    sleeps = await _run_poller_capture_sleep(monkeypatch, _hass_one_iter(), api)
    api.poll_commands.assert_awaited_once()
    assert sleeps == [600.0]


@pytest.mark.asyncio
async def test_loop_defaults_to_floor_when_no_interval(monkeypatch):
    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": []})
    sleeps = await _run_poller_capture_sleep(monkeypatch, _hass_one_iter(), api)
    assert sleeps == [float(COMMAND_POLL_INTERVAL_S)]


@pytest.mark.asyncio
async def test_loop_stops_on_device_evicted(monkeypatch):
    api = MagicMock()
    api.poll_commands = AsyncMock(side_effect=DeviceEvicted("revoked"))
    hass = MagicMock()
    type(hass).is_stopping = property(lambda _self: False)  # would loop forever if not for the stop
    sleeps = await _run_poller_capture_sleep(monkeypatch, hass, api)
    api.poll_commands.assert_awaited_once()
    assert sleeps == []


@pytest.mark.asyncio
async def test_loop_floors_fast_value_to_configured_interval(monkeypatch):
    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [], "pollIntervalMs": 1_000})
    sleeps = await _run_poller_capture_sleep(monkeypatch, _hass_one_iter(), api, interval_s=30)
    assert sleeps == [30.0]


@pytest.mark.asyncio
async def test_loop_stops_on_device_stopped(monkeypatch):
    """DeviceStopped raised by poll_commands → loop exits immediately (no sleep)."""
    api = MagicMock()
    api.poll_commands = AsyncMock(side_effect=DeviceStopped("manual eviction"))
    hass = MagicMock()
    type(hass).is_stopping = property(lambda _self: False)  # would loop forever if not for the stop
    sleeps = await _run_poller_capture_sleep(monkeypatch, hass, api)
    api.poll_commands.assert_awaited_once()
    assert sleeps == []


# ---------------------------------------------------------------------------
# Task 4: lifecycle wiring — poller sets lifecycle on eviction/stop
# ---------------------------------------------------------------------------


class _Store:
    async def set_lifecycle(self, *a) -> None:
        return None


@pytest.mark.asyncio
async def test_poller_410_sets_deprovisioned(monkeypatch):
    """DeviceEvicted (410) → lifecycle transitions to DEPROVISIONED before returning."""
    from custom_components.svitgrid.lifecycle import DEPROVISIONED, LifecycleState

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    api = MagicMock()
    api.poll_commands = AsyncMock(side_effect=DeviceEvicted("revoked"))
    hass = MagicMock()
    type(hass).is_stopping = property(lambda _self: False)
    lc = LifecycleState()
    store = _Store()
    kwargs = dict(
        hass=hass,
        api_client=api,
        keystore=None,
        entry_data=_entry_data(),
        wake_event=None,
        lifecycle=lc,
        store=store,
    )
    await poller_run_loop(**kwargs)
    api.poll_commands.assert_awaited_once()
    assert lc.state == DEPROVISIONED


@pytest.mark.asyncio
async def test_poller_stopped_sets_paused(monkeypatch):
    """DeviceStopped → lifecycle transitions to PAUSED before returning."""
    from custom_components.svitgrid.lifecycle import PAUSED, LifecycleState

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    api = MagicMock()
    api.poll_commands = AsyncMock(side_effect=DeviceStopped("disabled"))
    hass = MagicMock()
    type(hass).is_stopping = property(lambda _self: False)
    lc = LifecycleState()
    store = _Store()
    kwargs = dict(
        hass=hass,
        api_client=api,
        keystore=None,
        entry_data=_entry_data(),
        wake_event=None,
        lifecycle=lc,
        store=store,
    )
    await poller_run_loop(**kwargs)
    api.poll_commands.assert_awaited_once()
    assert lc.state == PAUSED


@pytest.mark.asyncio
async def test_set_cloud_endpoint_success_path_acks_then_applies(hass):
    """Success path (probe OK): probe → ACK success on original endpoint →
    apply. The cloud sees `success` on the ORIGINAL endpoint before the reload
    repoints us. Mirrors firmware D5's ack-restart-race fix. E-fix adds the
    probe step before the ACK so the ACK reflects truth."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from cryptography.hazmat.primitives import serialization
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()
    call_order: list[str] = []

    async def record_ack(*args, **kwargs):
        call_order.append("ack")
    api_client.ack_command.side_effect = record_ack

    def record_apply(*args, **kwargs):
        call_order.append("apply")

    with patch(
        "custom_components.svitgrid.command_poller.probe_endpoint_auth",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
        side_effect=record_apply,
    ) as mock_apply:
        await process_command(
            command={
                "commandId": "c1",
                "command": "set_cloud_endpoint",
                "payload": {"url": "https://api.svitgrid.app"},
            },
            api_client=api_client,
            api_key="k",
            trusted_public_keys_hex={},
            our_private_key=priv,
            our_signing_key_id="ours",
            executor_version="0.3.0",
            keystore=None,
            hass=hass,
            entry=MagicMock(entry_id="e1"),
        )

    assert call_order == ["ack", "apply"], (
        "Must ACK on the original endpoint BEFORE applying the change "
        "(else cmd.status stays at `delivered` forever — firmware D5 bug)"
    )
    api_client.ack_command.assert_awaited_once()
    ack_call = api_client.ack_command.await_args
    assert ack_call.kwargs["body"]["success"] is True
    mock_apply.assert_called_once()


@pytest.mark.asyncio
async def test_set_cloud_endpoint_rejects_disallowed_url(hass):
    """Disallowed URL: ACK as rejected, do NOT apply."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, _pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()

    with patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
    ) as mock_apply:
        await process_command(
            command={
                "commandId": "c-bad",
                "command": "set_cloud_endpoint",
                "payload": {"url": "https://evil.example.com"},
            },
            api_client=api_client,
            api_key="k",
            trusted_public_keys_hex={},
            our_private_key=priv,
            our_signing_key_id="ours",
            executor_version="0.3.0",
            keystore=None,
            hass=hass,
            entry=MagicMock(entry_id="e1"),
        )

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True
    assert body["reason"] == "disallowed_url"
    mock_apply.assert_not_called()


@pytest.mark.asyncio
async def test_set_cloud_endpoint_missing_payload_url_is_rejected(hass):
    """Malformed command (no payload.url) — ACK rejected, no apply."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, _pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()

    with patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
    ) as mock_apply:
        await process_command(
            command={
                "commandId": "c-empty",
                "command": "set_cloud_endpoint",
                "payload": {},
            },
            api_client=api_client,
            api_key="k",
            trusted_public_keys_hex={},
            our_private_key=priv,
            our_signing_key_id="ours",
            executor_version="0.3.0",
            keystore=None,
            hass=hass,
            entry=MagicMock(entry_id="e1"),
        )

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True
    assert body["reason"] == "disallowed_url"
    mock_apply.assert_not_called()


@pytest.mark.asyncio
async def test_set_cloud_endpoint_apply_failure_after_ack_is_swallowed_and_logged(
    hass, caplog,
):
    """If apply raises AFTER we've probed + ACKed success, swallow + log so
    the poller keeps running. The cloud already thinks the migration succeeded;
    operator must manually reconcile via grep of the distinctive log line."""
    import logging
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, _pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()

    with patch(
        "custom_components.svitgrid.command_poller.probe_endpoint_auth",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
        side_effect=RuntimeError("boom"),
    ) as mock_apply:
        with caplog.at_level(logging.ERROR, logger="custom_components.svitgrid.command_poller"):
            # Should NOT raise — apply failure is swallowed.
            await process_command(
                command={
                    "commandId": "c-boom",
                    "command": "set_cloud_endpoint",
                    "payload": {"url": "https://api.svitgrid.app"},
                },
                api_client=api_client,
                api_key="k",
                trusted_public_keys_hex={},
                our_private_key=priv,
                our_signing_key_id="ours",
                executor_version="0.3.0",
                keystore=None,
                hass=hass,
                entry=MagicMock(entry_id="e1"),
            )

    # ACK was sent with success=True (BEFORE the apply failure)
    api_client.ack_command.assert_awaited_once()
    assert api_client.ack_command.await_args.kwargs["body"]["success"] is True

    # apply was attempted (and raised)
    mock_apply.assert_called_once()

    # Distinctive error log for operator grep
    assert any(
        "set_cloud_endpoint apply failed" in record.message
        and record.levelno == logging.ERROR
        for record in caplog.records
    ), f"Expected distinctive error log not found. Records: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# E-fix: pre-flight probe integration with Arm 1c ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_cloud_endpoint_rejects_on_probe_failure(hass):
    """When probe_endpoint_auth returns False, Arm 1c must ACK rejected with
    reason='probe_failed' and must NOT call apply_cloud_endpoint_change."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, _pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()

    with patch(
        "custom_components.svitgrid.command_poller.probe_endpoint_auth",
        new_callable=AsyncMock,
        return_value=False,
    ) as mock_probe, patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
    ) as mock_apply:
        await process_command(
            command={
                "commandId": "c-probe-fail",
                "command": "set_cloud_endpoint",
                "payload": {"url": "https://api.svitgrid.app"},
            },
            api_client=api_client,
            api_key="k",
            trusted_public_keys_hex={},
            our_private_key=priv,
            our_signing_key_id="ours",
            executor_version="0.3.0",
            keystore=None,
            hass=hass,
            entry=MagicMock(entry_id="e1"),
        )

    # Probe must have been called
    mock_probe.assert_awaited_once()
    # ACK must be rejected with reason=probe_failed
    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True
    assert body["reason"] == "probe_failed"
    # apply must NOT have been called
    mock_apply.assert_not_called()


@pytest.mark.asyncio
async def test_set_cloud_endpoint_success_path_probes_before_acking(hass):
    """Restructured Arm 1c ordering: probe → ACK success → apply.
    When probe succeeds, ACK is sent with success=True and apply is called."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, _pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()
    call_order: list[str] = []

    async def record_probe(*args, **kwargs):
        call_order.append("probe")
        return True

    async def record_ack(*args, **kwargs):
        call_order.append("ack")

    def record_apply(*args, **kwargs):
        call_order.append("apply")

    api_client.ack_command.side_effect = record_ack

    with patch(
        "custom_components.svitgrid.command_poller.probe_endpoint_auth",
        side_effect=record_probe,
    ) as mock_probe, patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
        side_effect=record_apply,
    ) as mock_apply:
        await process_command(
            command={
                "commandId": "c-probe-ok",
                "command": "set_cloud_endpoint",
                "payload": {"url": "https://api.svitgrid.app"},
            },
            api_client=api_client,
            api_key="k",
            trusted_public_keys_hex={},
            our_private_key=priv,
            our_signing_key_id="ours",
            executor_version="0.3.0",
            keystore=None,
            hass=hass,
            entry=MagicMock(entry_id="e1"),
        )

    # Ordering: probe → ack → apply
    assert call_order == ["probe", "ack", "apply"], (
        f"Expected probe→ack→apply ordering, got {call_order}"
    )
    mock_probe.assert_awaited_once()
    api_client.ack_command.assert_awaited_once()
    ack_body = api_client.ack_command.await_args.kwargs["body"]
    assert ack_body["success"] is True
    mock_apply.assert_called_once()


async def test_set_cloud_endpoint_rejects_when_no_hass_or_entry(hass):
    """YAML-config installs (no ConfigEntry) must ACK rejected, not success.
    Otherwise the cloud audit log lies about migration completion."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    priv, _pub_hex = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()

    with patch(
        "custom_components.svitgrid.command_poller.apply_cloud_endpoint_change",
    ) as mock_apply:
        await process_command(
            command={
                "commandId": "c-yaml",
                "command": "set_cloud_endpoint",
                "payload": {"url": "https://api.svitgrid.app"},
            },
            api_client=api_client,
            api_key="k",
            trusted_public_keys_hex={},
            our_private_key=priv,
            our_signing_key_id="ours",
            executor_version="0.3.0",
            keystore=None,
            hass=None,
            entry=None,
        )

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False, (
        "YAML-path must NOT ack success — that lies to the cloud audit log "
        "about migration completion"
    )
    assert body["rejected"] is True
    assert body["reason"] == "yaml_config_no_entry"
    mock_apply.assert_not_called()
