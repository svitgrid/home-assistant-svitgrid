"""The integration's own hourly log file (collector-logs spec, Stage 1)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from custom_components.svitgrid.file_log import (
    HourlyFileHandler,
    hour_label,
    hour_path,
    redact,
)

NOW = datetime(2026, 11, 12, 14, 5, 0, tzinfo=timezone.utc).timestamp()


def _logger(name: str, handler: logging.Handler) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    return log


def test_hour_label_and_path_are_utc():
    assert hour_label(NOW) == "2026-11-12T14"
    assert hour_path("/r", "2026-11-12T14") == "/r/2026/11/12/2026-11-12T14.log"


def test_writes_each_record_to_its_utc_hour_file(tmp_path):
    h = HourlyFileHandler(str(tmp_path), now=lambda: NOW)
    log = _logger("custom_components.svitgrid.command_poller", h)
    try:
        log.info("poll ok")
        h.flush()
        text = open(hour_path(str(tmp_path), "2026-11-12T14")).read()
        assert text == "2026-11-12T14:05:00.000Z I svitgrid.command_poller poll ok\n"
    finally:
        log.removeHandler(h)
        h.close()


def test_warning_and_error_letters(tmp_path):
    h = HourlyFileHandler(str(tmp_path), now=lambda: NOW)
    log = _logger("custom_components.svitgrid.x", h)
    try:
        log.warning("w")
        log.error("e")
        h.flush()
        lines = open(hour_path(str(tmp_path), "2026-11-12T14")).read().splitlines()
        assert [ln.split(" ")[1] for ln in lines] == ["W", "E"]
    finally:
        log.removeHandler(h)
        h.close()


def test_debug_is_not_written(tmp_path):
    h = HourlyFileHandler(str(tmp_path), now=lambda: NOW)
    log = _logger("custom_components.svitgrid.dbg", h)
    try:
        log.debug("noise")
        h.flush()
        assert not os.path.exists(hour_path(str(tmp_path), "2026-11-12T14"))
    finally:
        log.removeHandler(h)
        h.close()


def test_install_attaches_to_our_logger_only(tmp_path):
    from custom_components.svitgrid.file_log import attach, detach

    h = attach(str(tmp_path), now=lambda: NOW)
    try:
        logging.getLogger("custom_components.svitgrid.a").info("ours")
        logging.getLogger("homeassistant.components.other").warning("theirs")
        h.flush()
        text = open(hour_path(str(tmp_path), "2026-11-12T14")).read()
        assert "ours" in text and "theirs" not in text
    finally:
        detach(h)


def test_prune_deletes_hours_older_than_7_days(tmp_path):
    root = str(tmp_path)
    for label in ("2026-11-04T13", "2026-11-05T15"):
        p = hour_path(root, label)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("x\n")
    h = HourlyFileHandler(root, now=lambda: NOW)
    try:
        now = datetime(2026, 11, 12, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        assert h.prune(now) == 1
        assert not os.path.exists(hour_path(root, "2026-11-04T13"))
        assert os.path.exists(hour_path(root, "2026-11-05T15"))
    finally:
        h.close()


def test_unwritable_root_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    h = HourlyFileHandler(str(blocker), now=lambda: NOW)
    log = _logger("custom_components.svitgrid.unwritable", h)
    try:
        log.info("still fine")
        h.flush()
        h.prune(NOW)
    finally:
        log.removeHandler(h)
        h.close()


def test_redacts_keys_and_tokens():
    assert "abc123" not in redact("x-api-key: abc123")
    assert "abc123" not in redact("api_key=abc123 more")
    assert "abc123" not in redact('{"apiKey": "abc123"}')
    assert "abc123" not in redact("Authorization: Bearer abc123")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    assert "eyJhbGciOi" not in redact(f"token {jwt} end")
    assert redact("plain line") == "plain line"


def test_written_lines_are_redacted(tmp_path):
    h = HourlyFileHandler(str(tmp_path), now=lambda: NOW)
    log = _logger("custom_components.svitgrid.leak", h)
    try:
        log.info("headers x-api-key: sekret42")
        h.flush()
        assert "sekret42" not in open(hour_path(str(tmp_path), "2026-11-12T14")).read()
    finally:
        log.removeHandler(h)
        h.close()


async def test_ensure_file_log_attaches_once_under_config_svitgrid_logs(hass):
    from custom_components.svitgrid.const import DOMAIN
    from custom_components.svitgrid.file_log import async_ensure_file_log, detach

    first = await async_ensure_file_log(hass)
    second = await async_ensure_file_log(hass)
    try:
        assert first is second
        assert hass.data[DOMAIN]["file_log"] is first
        assert first._root == hass.config.path("svitgrid", "logs")
        ours = [h for h in logging.getLogger("custom_components.svitgrid").handlers if h is first]
        assert len(ours) == 1
    finally:
        await hass.async_add_executor_job(detach, first)
