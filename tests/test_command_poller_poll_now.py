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

import asyncio
import time

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


# --- upload_logs (collector-logs spec, Stage 1) ---------------------------

import time  # noqa: E402
from unittest.mock import patch  # noqa: E402


def test_upload_logs_is_an_internal_command():
    from custom_components.svitgrid.const import UPLOAD_LOGS_COMMAND

    assert UPLOAD_LOGS_COMMAND == "upload_logs"
    assert UPLOAD_LOGS_COMMAND in INTERNAL_COMMANDS


async def _process_upload_logs(hass, payload: dict) -> MagicMock:
    from custom_components.svitgrid.const import DOMAIN

    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    hass.data.setdefault(DOMAIN, {})["file_log"] = MagicMock()
    await process_command(
        command={"commandId": "c-log", "command": "upload_logs", "payload": payload},
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=None,
        hass=hass,
        entry=None,
    )
    return api_client


async def test_upload_logs_unsigned_is_acked_started(hass):
    now = int(time.time())
    with patch(
        "custom_components.svitgrid.command_poller.run_upload", new=AsyncMock(return_value={})
    ) as run:
        api_client = await _process_upload_logs(hass, {"from": now - 3600, "to": now})
        await hass.async_block_till_done()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is True
    assert body["result"] == {"started": True}
    assert run.await_args.kwargs["command_id"] == "c-log"
    assert run.await_args.kwargs["api_key"] == "k"


async def test_upload_logs_bad_window_is_acked_failed(hass):
    now = int(time.time())
    with patch(
        "custom_components.svitgrid.command_poller.run_upload", new=AsyncMock(return_value={})
    ) as run:
        api_client = await _process_upload_logs(hass, {"from": now - 90000, "to": now})
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["result"] == {"error": "window_too_long"}
    run.assert_not_awaited()


async def test_upload_logs_busy_while_one_runs(hass):
    from custom_components.svitgrid.log_upload import _UPLOAD_LOCK

    now = int(time.time())
    await _UPLOAD_LOCK.acquire()
    try:
        api_client = await _process_upload_logs(hass, {"from": now - 3600, "to": now})
    finally:
        _UPLOAD_LOCK.release()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["result"] == {"error": "busy"}


async def test_cloud_upload_logs_mid_send_is_busy(hass):
    """An island send holds the lock while it waits on the box: a cloud
    upload_logs arriving then is refused busy."""
    from unittest.mock import MagicMock

    from custom_components.svitgrid.island_send import IslandSendRelay, run_island_send

    api = MagicMock()
    api.island_logs_start = AsyncMock(side_effect=[[], [{"commandId": "b0", "from": 1, "to": 2}]])
    release = asyncio.Event()

    async def wait(*_):
        await release.wait()

    queue = MagicMock()
    queue.async_enqueue = AsyncMock(return_value="l")
    queue.async_wait = wait
    task = asyncio.ensure_future(
        run_island_send(
            api_client=api, api_key="k", root="/r", flush=lambda: None,
            now_ts=int(time.time()), box_id="edge-1", queue=queue, relay=IslandSendRelay(),
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
    now = int(time.time())
    try:
        api_client = await _process_upload_logs(hass, {"from": now - 3600, "to": now})
    finally:
        release.set()
        await task
    assert api_client.ack_command.await_args.kwargs["body"]["result"] == {"error": "busy"}
