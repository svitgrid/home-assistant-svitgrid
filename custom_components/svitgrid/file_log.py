"""The integration's own log, kept in hourly UTC files for 7 days.

An operator pulls a window of it remotely with the `upload_logs` command, the
same way as from an ED-1's microSD card (see log_upload.py). One set of files
per Home Assistant install, shared by every Svitgrid config entry.

Records are queued by `emit` and written by a background thread, so Home
Assistant's event loop never waits on the disk. A failed write is dropped
silently: logging it would feed the failure back into this handler.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

LOGGER_NAME = "custom_components.svitgrid"
LOG_SUBDIR = "logs"
RETAIN_HOURS = 7 * 24

_LEVEL_LETTER = {logging.INFO: "I", logging.WARNING: "W", logging.ERROR: "E", logging.CRITICAL: "E"}
_PREFIX = "custom_components."

_REDACTIONS = [
    re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    re.compile(r"(?i)(api_?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s\"',}]+"),
]
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


def redact(msg: str) -> str:
    """Replace API keys, bearer tokens and JWTs with ***."""
    for pattern in _REDACTIONS:
        msg = pattern.sub(r"\1***", msg)
    return _JWT.sub("***", msg)


def hour_label(ts: float) -> str:
    """The UTC hour a timestamp falls in, as YYYY-MM-DDTHH."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H")


def hour_path(root: str, label: str) -> str:
    """root/YYYY/MM/DD/YYYY-MM-DDTHH.log"""
    return os.path.join(root, label[0:4], label[5:7], label[8:10], f"{label}.log")


def _label_ts(label: str) -> float:
    return datetime.strptime(label, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp()


class HourlyFileHandler(logging.Handler):
    """Writes INFO and above to hourly UTC files under `root`."""

    def __init__(self, root: str, now: Callable[[], float] = time.time) -> None:
        super().__init__(level=logging.INFO)
        self._root = root
        self._now = now
        self._queue: queue.SimpleQueue[tuple[str, str] | threading.Event | None] = queue.SimpleQueue()
        self._last_label: str | None = None
        self._thread = threading.Thread(target=self._run, name="svitgrid-file-log", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = self._now()
            stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            name = record.name[len(_PREFIX):] if record.name.startswith(_PREFIX) else record.name
            letter = _LEVEL_LETTER.get(record.levelno, "I")
            line = f"{stamp}Z {letter} {name} {redact(record.getMessage())}\n"
            self._queue.put((hour_label(ts), line))
        except Exception:  # noqa: BLE001 - a log call must never raise into HA
            pass

    def flush(self) -> None:
        """Wait until every queued line is on disk (or dropped)."""
        if not self._thread.is_alive():
            return
        marker = threading.Event()
        self._queue.put(marker)
        marker.wait(timeout=5)

    def close(self) -> None:
        if self._thread.is_alive():
            self.flush()
            self._queue.put(None)
            self._thread.join(timeout=5)
        super().close()

    def prune(self, now_ts: float) -> int:
        """Delete hour files older than RETAIN_HOURS. Returns how many."""
        cutoff = now_ts - RETAIN_HOURS * 3600
        deleted = 0
        try:
            for dirpath, _dirs, files in os.walk(self._root):
                for name in files:
                    if not name.endswith(".log"):
                        continue
                    try:
                        if _label_ts(name[:-4]) < cutoff:
                            os.remove(os.path.join(dirpath, name))
                            deleted += 1
                    except (ValueError, OSError):
                        continue
        except OSError:
            pass
        return deleted

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            label, line = item
            if label != self._last_label:
                self._last_label = label
                self.prune(_label_ts(label))
            try:
                path = hour_path(self._root, label)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


def attach(root: str, now: Callable[[], float] = time.time) -> HourlyFileHandler:
    """Attach a handler to the integration's logger and prune once."""
    handler = HourlyFileHandler(root, now=now)
    handler.prune(now())
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    return handler


def detach(handler: HourlyFileHandler) -> None:
    logging.getLogger(LOGGER_NAME).removeHandler(handler)
    handler.close()


async def async_ensure_file_log(hass) -> HourlyFileHandler:
    """Attach the install's one handler, once. Disk work runs in the executor."""
    from .const import DOMAIN, READINGS_DB_SUBDIR

    data = hass.data.setdefault(DOMAIN, {})
    handler = data.get("file_log")
    if handler is None:
        root = hass.config.path(READINGS_DB_SUBDIR, LOG_SUBDIR)
        handler = await hass.async_add_executor_job(attach, root)
        data["file_log"] = handler

        async def _on_stop(_event) -> None:
            # Stop the writer thread with Home Assistant, after a last flush.
            if data.get("file_log") is handler:
                data.pop("file_log", None)
            await hass.async_add_executor_job(detach, handler)

        from homeassistant.const import EVENT_HOMEASSISTANT_STOP

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    return handler
