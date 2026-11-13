"""The island send: HA uploads its own 7 days, then relays its ED-1 box's
last 24 hours, all under the one upload lock."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .api_client import IslandLogsRefused, SvitgridApiError
from .edge_command_queue import _DONE_STATES
from .log_upload import UPLOAD_LOCK, upload_window

_LOGGER = logging.getLogger(__name__)

WEEK_S = 7 * 86400
DAY_S = 86400
BOX_TTL_S = 1800
BOX_WAIT_S = 600


class IslandSendRelay:
    """Maps the box's local command id to the cloud command id of its slice."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def remember(self, local_id: str, cloud_id: str) -> None:
        self._ids[local_id] = cloud_id

    def cloud_id_for(self, local_id: str) -> str | None:
        return self._ids.get(local_id)

    def forget(self, local_id: str) -> None:
        self._ids.pop(local_id, None)


async def run_island_send(
    *,
    api_client: Any,
    api_key: str,
    root: str,
    flush: Callable[[], None],
    now_ts: int,
    box_id: str | None,
    queue: Any,
    relay: IslandSendRelay,
) -> None:
    """Send HA's week, then the box's day. Never raises on a refusal."""
    async with UPLOAD_LOCK:
        try:
            try:
                slices = await api_client.island_logs_start(
                    api_key=api_key, from_ts=now_ts - WEEK_S, to_ts=now_ts
                )
            except IslandLogsRefused as err:
                if err.error != "already_sent":
                    raise
                _LOGGER.info("island log send: own week already sent, continuing to the box")
                slices = []
            for item in slices:
                summary = await upload_window(
                    api_client=api_client, api_key=api_key, command_id=item["commandId"],
                    from_ts=item["from"], to_ts=item["to"], root=root, flush=flush,
                )
                if summary.get("error"):
                    break
            if not box_id:
                return
            slices = await api_client.island_logs_start(
                api_key=api_key, from_ts=now_ts - DAY_S, to_ts=now_ts, for_device_id=box_id
            )
            for item in slices:
                local = await queue.async_enqueue(
                    box_id, "upload_logs", {"from": item["from"], "to": item["to"]},
                    signing=None, inverter_id=None, source="island_logs", ttl_s=BOX_TTL_S,
                )
                relay.remember(local, item["commandId"])
                row = await queue.async_wait(local, BOX_WAIT_S)
                state = None if row is None else row.get("state")
                if state == "failed":
                    _LOGGER.warning(
                        "island log send: box %s failed slice %s", box_id, item["commandId"]
                    )
                elif state not in _DONE_STATES:
                    _LOGGER.warning(
                        "island log send: box %s did not answer slice %s in time",
                        box_id, item["commandId"],
                    )
        except IslandLogsRefused as err:
            _LOGGER.warning("island log send refused: %s", err.error)
        except SvitgridApiError as err:
            _LOGGER.warning("island log send failed: %s", type(err).__name__)
        except Exception as err:  # noqa: BLE001 - a background task must not raise into HA
            _LOGGER.warning("island log send failed: %s", type(err).__name__)
