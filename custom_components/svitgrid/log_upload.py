"""upload_logs: send a window of the integration's hourly log to the cloud.

The same command, rules and bodies as the ED-1's microSD log
(services/api/src/models/schemas.ts, EdgeDeviceLogPartSchema and
EdgeDeviceLogSummarySchema): a window of at most 24 h, parts of at most
32 KB posted to POST /api/v1/edge-devices/logs, then one summary.
"""

from __future__ import annotations

from typing import Any

from .file_log import hour_label, hour_path

MAX_WINDOW_S = 86400
PART_MAX_BYTES = 32768


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


def read_parts(root: str, label: str) -> list[str]:
    """The hour's text in parts of at most PART_MAX_BYTES, split at line ends.

    [] when the hour has no file.
    """
    try:
        with open(hour_path(root, label), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
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

