"""The island send: HA's own week, then its box's last day, under one lock."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.api_client import IslandLogsRefused, SvitgridApiError
from custom_components.svitgrid.island_send import IslandSendRelay, run_island_send
from custom_components.svitgrid.log_upload import UPLOAD_LOCK

NOW = 1_800_000_000


def _slices(prefix, start, n=1):
    return [{"commandId": f"{prefix}{i}", "from": start + i, "to": start + i + 1} for i in range(n)]


def _run(api, queue, relay, box_id=None, upload=None):
    return run_island_send(
        api_client=api, api_key="k", root="/r", flush=lambda: None, now_ts=NOW,
        box_id=box_id, queue=queue, relay=relay,
    )


def _api(*results):
    api = MagicMock()
    api.island_logs_start = AsyncMock(side_effect=list(results))
    return api


async def test_own_week_uploads_seven_slices_in_order():
    api = _api(_slices("c", NOW - 604800, 7))
    with patch(
        "custom_components.svitgrid.island_send.upload_window",
        new=AsyncMock(return_value={"done": True}),
    ) as up:
        await _run(api, MagicMock(), IslandSendRelay())
    kw = api.island_logs_start.await_args.kwargs
    assert (kw["from_ts"], kw["to_ts"]) == (NOW - 604800, NOW)
    assert [c.kwargs["command_id"] for c in up.await_args_list] == [f"c{i}" for i in range(7)]


async def test_own_week_stops_at_first_error():
    api = _api(_slices("c", 0, 3))
    with patch(
        "custom_components.svitgrid.island_send.upload_window",
        new=AsyncMock(side_effect=[{"done": True}, {"error": "refused"}, {}]),
    ) as up:
        await _run(api, MagicMock(), IslandSendRelay())
    assert up.await_count == 2


async def test_box_slice_is_enqueued_and_mapped():
    api = _api([], _slices("b", NOW - 86400))
    queue = MagicMock()
    queue.async_enqueue = AsyncMock(return_value="local-1")
    queue.async_wait = AsyncMock(return_value={"state": "success"})
    relay = IslandSendRelay()
    with patch("custom_components.svitgrid.island_send.upload_window", new=AsyncMock()):
        await _run(api, queue, relay, box_id="edge-1")
    kw = api.island_logs_start.await_args_list[1].kwargs
    assert kw["for_device_id"] == "edge-1"
    assert (kw["from_ts"], kw["to_ts"]) == (NOW - 86400, NOW)
    args, ekw = queue.async_enqueue.await_args
    assert args == ("edge-1", "upload_logs", {"from": NOW - 86400, "to": NOW - 86399})
    assert ekw["signing"] is None and ekw["inverter_id"] is None
    assert ekw["source"] == "island_logs" and ekw["ttl_s"] == 1800
    queue.async_wait.assert_awaited_once_with("local-1", 600)
    assert relay.cloud_id_for("local-1") == "b0"
    relay.forget("local-1")
    assert relay.cloud_id_for("local-1") is None


async def test_closed_uploads_nothing_and_logs_warning(caplog):
    api = MagicMock()
    api.island_logs_start = AsyncMock(side_effect=IslandLogsRefused("closed"))
    queue = MagicMock()
    with patch("custom_components.svitgrid.island_send.upload_window", new=AsyncMock()) as up:
        with caplog.at_level(logging.WARNING):
            await _run(api, queue, IslandSendRelay(), box_id="edge-1")
    up.assert_not_awaited()
    assert api.island_logs_start.await_count == 1
    assert "island log send refused: closed" in caplog.text
    assert not UPLOAD_LOCK.locked()


async def test_lock_held_for_the_whole_run():
    api = _api(_slices("c", 0, 1), _slices("b", 0, 1))
    seen = []

    async def up(**_):
        seen.append(UPLOAD_LOCK.locked())
        return {}

    async def wait(*_):
        seen.append(UPLOAD_LOCK.locked())
        return {}

    queue = MagicMock()
    queue.async_enqueue = AsyncMock(return_value="l")
    queue.async_wait = wait
    with patch("custom_components.svitgrid.island_send.upload_window", new=up):
        await _run(api, queue, IslandSendRelay(), box_id="edge-1")
    assert seen == [True, True]
    assert not UPLOAD_LOCK.locked()


async def test_box_silent_releases_lock_after_wait(caplog):
    api = _api([], _slices("b", 0, 1))
    queue = MagicMock()
    queue.async_enqueue = AsyncMock(return_value="l")
    queue.async_wait = AsyncMock(return_value=None)
    with patch("custom_components.svitgrid.island_send.upload_window", new=AsyncMock()):
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(_run(api, queue, IslandSendRelay(), box_id="edge-1"), 5)
    assert not UPLOAD_LOCK.locked()
    assert "edge-1" in caplog.text


async def test_failed_box_ack_is_logged_apart_from_a_timeout(caplog):
    api = _api([], _slices("b", 0, 1))
    queue = MagicMock()
    queue.async_enqueue = AsyncMock(return_value="l")
    queue.async_wait = AsyncMock(return_value={"state": "failed"})
    with patch("custom_components.svitgrid.island_send.upload_window", new=AsyncMock()):
        with caplog.at_level(logging.WARNING):
            await _run(api, queue, IslandSendRelay(), box_id="edge-1")
    assert "failed slice" in caplog.text and "in time" not in caplog.text


@pytest.mark.parametrize("exc", [SvitgridApiError("HTTP 500"), OSError("down")])
async def test_api_or_network_error_never_escapes(exc):
    api = MagicMock()
    api.island_logs_start = AsyncMock(side_effect=exc)
    with patch("custom_components.svitgrid.island_send.upload_window", new=AsyncMock()):
        await _run(api, MagicMock(), IslandSendRelay(), box_id="edge-1")
    assert not UPLOAD_LOCK.locked()
