"""Reading a window of the integration's hourly log for upload_logs."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from custom_components.svitgrid.file_log import hour_path
from custom_components.svitgrid.log_upload import (
    MAX_WINDOW_S,
    PART_MAX_BYTES,
    hours_in_window,
    read_parts,
    window_error,
)


def _ts(*args: int) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_constants_match_the_edge():
    assert MAX_WINDOW_S == 86400
    assert PART_MAX_BYTES == 32768


def test_window_rules():
    now = 1794500000
    assert window_error({"from": now - 3600, "to": now}, now) is None
    assert window_error({"from": now - 86400, "to": now}, now) is None
    assert window_error({"from": now - 86401, "to": now}, now) == "window_too_long"
    assert window_error({"from": now, "to": now}, now) == "window_empty"
    assert window_error({"from": now + 60, "to": now + 120}, now) == "window_empty"
    assert window_error({"from": "x", "to": now}, now) == "bad_payload"
    assert window_error({"from": True, "to": now}, now) == "bad_payload"
    assert window_error({}, now) == "bad_payload"
    assert window_error(None, now) == "bad_payload"


def test_hours_in_window_spans_partial_hours():
    assert hours_in_window(_ts(2026, 11, 12, 14, 30), _ts(2026, 11, 12, 16, 10)) == [
        "2026-11-12T14",
        "2026-11-12T15",
        "2026-11-12T16",
    ]


def test_hours_in_window_excludes_an_hour_the_window_only_touches_at_its_end():
    assert hours_in_window(_ts(2026, 11, 12, 14), _ts(2026, 11, 12, 15)) == ["2026-11-12T14"]


def _write(root: str, label: str, text: str) -> None:
    p = hour_path(root, label)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def test_read_parts_splits_on_line_boundaries_under_32k(tmp_path):
    line = "x" * 99 + "\n"
    text = line * 1000
    _write(str(tmp_path), "2026-11-12T14", text)
    parts = read_parts(str(tmp_path), "2026-11-12T14")
    assert len(parts) > 1
    assert all(len(p.encode()) <= PART_MAX_BYTES for p in parts)
    assert all(p.endswith("\n") for p in parts)
    assert "".join(parts) == text


def test_read_parts_splits_an_overlong_line_on_a_character_boundary(tmp_path):
    text = "П" * 20000 + "\n"  # 40000 bytes in one line
    _write(str(tmp_path), "2026-11-12T14", text)
    parts = read_parts(str(tmp_path), "2026-11-12T14")
    assert all(len(p.encode()) <= PART_MAX_BYTES for p in parts)
    assert "".join(parts) == text


def test_read_parts_missing_file_is_empty(tmp_path):
    assert read_parts(str(tmp_path), "2026-11-12T03") == []
