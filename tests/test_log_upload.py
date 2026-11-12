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


class _FakeClient:
    def __init__(self, refuse_at: int | None = None) -> None:
        self.parts: list[dict] = []
        self.summaries: list[dict] = []
        self._refuse_at = refuse_at

    async def post_log_part(self, api_key: str, body: dict) -> None:
        from custom_components.svitgrid.api_client import LogUploadRefused

        assert api_key == "k1"
        if self._refuse_at is not None and len(self.parts) == self._refuse_at:
            raise LogUploadRefused("HTTP 403")
        self.parts.append(body)

    async def post_log_summary(self, api_key: str, body: dict) -> None:
        self.summaries.append(body)


async def test_run_upload_posts_parts_then_summary(tmp_path):
    from custom_components.svitgrid.log_upload import run_upload

    root = str(tmp_path)
    _write(root, "2026-11-12T14", ("y" * 99 + "\n") * 400)  # 40000 B -> 2 parts
    client = _FakeClient()
    calls: list[str] = []
    summary = await run_upload(
        api_client=client,
        api_key="k1",
        command_id="c1",
        from_ts=_ts(2026, 11, 12, 14),
        to_ts=_ts(2026, 11, 12, 16),
        root=root,
        flush=lambda: calls.append("flush"),
    )
    assert [(p["hour"], p["part"], p["commandId"]) for p in client.parts] == [
        ("2026-11-12T14", 0, "c1"),
        ("2026-11-12T14", 1, "c1"),
    ]
    assert client.summaries == [summary]
    assert summary == {
        "commandId": "c1",
        "done": True,
        "from": _ts(2026, 11, 12, 14),
        "to": _ts(2026, 11, 12, 16),
        "hours": [{"hour": "2026-11-12T14", "bytes": 40000, "parts": 2}],
        "missing": ["2026-11-12T15"],
        "dropped": 0,
        "lost": 0,
        "error": None,
    }


async def test_run_upload_flushes_before_reading(tmp_path):
    from custom_components.svitgrid.log_upload import run_upload

    root = str(tmp_path)
    client = _FakeClient()

    def flush() -> None:  # the line arrives only on flush, as from the writer thread
        _write(root, "2026-11-12T14", "late line\n")

    await run_upload(
        api_client=client, api_key="k1", command_id="c1",
        from_ts=_ts(2026, 11, 12, 14), to_ts=_ts(2026, 11, 12, 15),
        root=root, flush=flush,
    )
    assert [p["text"] for p in client.parts] == ["late line\n"]


async def test_run_upload_stops_on_403_and_reports(tmp_path):
    from custom_components.svitgrid.log_upload import run_upload

    root = str(tmp_path)
    _write(root, "2026-11-12T14", ("y" * 99 + "\n") * 400)
    _write(root, "2026-11-12T15", "z\n")
    client = _FakeClient(refuse_at=0)
    summary = await run_upload(
        api_client=client, api_key="k1", command_id="c1",
        from_ts=_ts(2026, 11, 12, 14), to_ts=_ts(2026, 11, 12, 16),
        root=root, flush=lambda: None,
    )
    assert client.parts == []
    assert len(client.summaries) == 1
    assert summary["error"] == "refused"


async def test_run_upload_holds_the_lock_while_running(tmp_path):
    from custom_components.svitgrid.log_upload import _UPLOAD_LOCK, run_upload

    seen: list[bool] = []

    class _Probe(_FakeClient):
        async def post_log_summary(self, api_key: str, body: dict) -> None:
            seen.append(_UPLOAD_LOCK.locked())

    await run_upload(
        api_client=_Probe(), api_key="k1", command_id="c1",
        from_ts=_ts(2026, 11, 12, 14), to_ts=_ts(2026, 11, 12, 15),
        root=str(tmp_path), flush=lambda: None,
    )
    assert seen == [True]
    assert not _UPLOAD_LOCK.locked()
