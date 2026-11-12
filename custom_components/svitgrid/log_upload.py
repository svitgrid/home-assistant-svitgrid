"""upload_logs: send a window of the integration's hourly log to the cloud.

The same command, rules and bodies as the ED-1's microSD log
(services/api/src/models/schemas.ts, EdgeDeviceLogPartSchema and
EdgeDeviceLogSummarySchema): a window of at most 24 h, parts of at most
32 KB posted to POST /api/v1/edge-devices/logs, then one summary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from datetime import datetime, timezone

from .file_log import hour_label, hour_path

_LOGGER = logging.getLogger(__name__)

MAX_WINDOW_S = 86400
PART_MAX_BYTES = 32768

# One upload at a time per install, as on the ED-1 (a second is refused busy).
_UPLOAD_LOCK = asyncio.Lock()


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def window_error(payload: Any, now_ts: float) -> str | None:
    """None when the window is acceptable, else the edge's refusal name."""
    if not isinstance(payload, dict):
        return "bad_payload"
    start, end = payload.get("from"), payload.get("to")
    if not _is_int(start) or not _is_int(end):
        return "bad_payload"
    if end - start > MAX_WINDOW_S:
        return "window_too_long"
    if end <= start or start > now_ts:
        return "window_empty"
    return None


def hours_in_window(from_ts: int, to_ts: int) -> list[str]:
    """The hour labels a [from, to) window touches, oldest first."""
    labels: list[str] = []
    hour = from_ts - from_ts % 3600
    while hour < to_ts:
        labels.append(hour_label(hour))
        hour += 3600
    return labels


def _split_line(line: str) -> list[str]:
    """Split one over-long line on character boundaries."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > PART_MAX_BYTES:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(ch)
        size += n
    if current:
        chunks.append("".join(current))
    return chunks


def _stamp(ts: int) -> str:
    """The line-prefix form of a timestamp: 2026-11-12T14:05:00.000Z."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _in_window(line: str, start: str, end: str) -> bool:
    # Lines start with a fixed-width UTC stamp, so string order is time order.
    # A line without one (never written by file_log) is kept.
    stamp = line[:24]
    if len(stamp) < 24 or stamp[10] != "T" or stamp[23] != "Z":
        return True
    return start <= stamp < end


def read_parts(
    root: str, label: str, from_ts: int | None = None, to_ts: int | None = None
) -> list[str]:
    """The hour's text in parts of at most PART_MAX_BYTES, split at line ends.

    With a window, only lines stamped inside [from_ts, to_ts) are kept, as the
    ED-1 does. [] when the hour has no file or no line in the window.
    """
    try:
        with open(hour_path(root, label), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    if from_ts is not None and to_ts is not None:
        start, end = _stamp(from_ts), _stamp(to_ts)
        text = "".join(ln for ln in text.splitlines(keepends=True) if _in_window(ln, start, end))
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        n = len(line.encode("utf-8"))
        if n > PART_MAX_BYTES:
            if current:
                parts.append("".join(current))
                current, size = [], 0
            parts.extend(_split_line(line))
            continue
        if size + n > PART_MAX_BYTES:
            parts.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += n
    if current:
        parts.append("".join(current))
    return parts



async def run_upload(
    *,
    api_client: Any,
    api_key: str,
    command_id: str,
    from_ts: int,
    to_ts: int,
    root: str,
    flush: Callable[[], None],
) -> dict[str, Any]:
    """Post the window's parts, then the summary. Returns the summary posted.

    A refused or failed part stops the upload; the summary still goes out once,
    with `error` set, so the operator's script is not left waiting.
    """
    from .api_client import LogUploadRefused

    async with _UPLOAD_LOCK:
        # Lines still queued in the writer thread belong to the current hour.
        await asyncio.to_thread(flush)
        hours: list[dict[str, Any]] = []
        missing: list[str] = []
        error: str | None = None
        for label in hours_in_window(from_ts, to_ts):
            parts = await asyncio.to_thread(read_parts, root, label, from_ts, to_ts)
            if not parts:
                missing.append(label)
                continue
            sent_bytes = 0
            for index, text in enumerate(parts):
                try:
                    await api_client.post_log_part(
                        api_key=api_key,
                        body={"commandId": command_id, "hour": label, "part": index, "text": text},
                    )
                except LogUploadRefused:
                    error = "refused"
                    break
                except Exception:  # noqa: BLE001 - stop and report, never retry forever
                    error = "post_failed"
                    break
                sent_bytes += len(text.encode("utf-8"))
            if error is not None:
                break
            hours.append({"hour": label, "bytes": sent_bytes, "parts": len(parts)})
        summary = {
            "commandId": command_id,
            "done": True,
            "from": from_ts,
            "to": to_ts,
            "hours": hours,
            "missing": missing,
            "dropped": 0,
            "lost": 0,
        }
        # The server's schema has `error: z.string().optional()`: a null is
        # refused with 400. The board, too, sends the key only on a failure.
        if error is not None:
            summary["error"] = error
        try:
            await api_client.post_log_summary(api_key=api_key, body=summary)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("upload_logs %s: the summary was not accepted", command_id)
        _LOGGER.info(
            "upload_logs %s: %d hours sent, %d missing%s",
            command_id, len(hours), len(missing), f", error {error}" if error else "",
        )
        return summary
