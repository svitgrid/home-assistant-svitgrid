"""Tests for the poll_now command handler.

poll_now is the app's "Refresh now" (queued by the API's inverter-refresh-now
endpoint into edgeDeviceCommands, device-targeted like the edge firmware's
poll_now). The HA add-on had NO handler for it, so it fell through to the
admin-signature gate and was dropped as an "unsigned non-internal command" —
leaving the command un-ACKed forever (edgeDevices.pendingCommandCount stuck > 0,
the poller re-fetching + re-skipping it every cycle) and making "Refresh now"
silently do nothing on HA households.

poll_now is now an internal (no-signature) command handled as a no-op that ACKs
success: the HA readings publisher already republishes on its own short cadence
(floor 5s), so acknowledging clears the counter and stops the skip-loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.const import INTERNAL_COMMANDS, POLL_NOW_COMMAND
from custom_components.svitgrid.signing import generate_keypair


def _make_api_client() -> MagicMock:
    c = MagicMock()
    c.ack_command = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_poll_now_acks_success_without_signature():
    """An unsigned poll_now is handled internally and ACKed success (NOT skipped).

    hass/entry/keystore are all None to prove the handler is a pure no-op that
    needs none of them.
    """
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()

    await process_command(
        command={
            "commandId": "c-poll",
            "command": "poll_now",
            "payload": {"inverterId": "ha-9f5224d86ee6"},
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

    # Success ACK sent (the command is no longer dropped as unsigned).
    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is True, f"Expected success ACK for poll_now, got: {body}"


def test_poll_now_is_an_internal_command():
    """poll_now must be in INTERNAL_COMMANDS — handled by the add-on itself,
    never dispatched to an executor and never requiring an admin signature."""
    assert POLL_NOW_COMMAND == "poll_now"
    assert POLL_NOW_COMMAND in INTERNAL_COMMANDS
