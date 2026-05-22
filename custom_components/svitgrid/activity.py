"""ActivityTracker — shared in-memory state feeding the Svitgrid HA
device-page sensors.

Hosts the most recent ingest + command outcomes so the user can see at
a glance whether the integration is healthy and what's been happening.
Two pieces of state per kind:
- An aggregated counter rolling 24h (powers `sensor.svitgrid_*_count_24h`)
- A deque of the last 10 events (exposed via sensor attribute dict)

Memory-only — restart clears history. That's fine: the API has the
authoritative reading + command history; this is just a fast-glance
status view inside HA.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

_RECENT_BUFFER_SIZE = 10
_COUNTER_WINDOW = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class ActivityTracker:
    """Mutable, single-writer shared object. readings_publisher and
    command_poller both call record_* methods; sensor.py reads."""

    now: Callable[[], datetime] = _utc_now

    # Most recent outcome — used as the `sensor.svitgrid_status` value.
    status: str = "idle"

    last_ingest_at: datetime | None = None
    last_ingest_status: str | None = None
    _ingest_times: deque[datetime] = field(default_factory=deque)
    _recent_ingests: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_BUFFER_SIZE)
    )

    last_command_at: datetime | None = None
    last_command_kind: str | None = None
    _command_times: deque[datetime] = field(default_factory=deque)
    _recent_commands: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_BUFFER_SIZE)
    )

    # ── ingest path ────────────────────────────────────────────────────

    def record_ingest_success(
        self,
        *,
        sample_count: int,
        period_sec: int,
        summary: dict[str, Any],
    ) -> None:
        """Called by readings_publisher after a 2xx ack from /ingest/reading.
        `summary` should contain a few headline fields (pvPower, loadPower,
        batterySoc) for at-a-glance status — full payload not stored."""
        now = self.now()
        self.status = "ok"
        self.last_ingest_at = now
        self.last_ingest_status = "ok"
        self._ingest_times.append(now)
        self._prune_window(self._ingest_times, now)
        self._recent_ingests.append({
            "at": now.isoformat(),
            "status": "ok",
            "sample_count": sample_count,
            "period_sec": period_sec,
            "summary": dict(summary),
        })

    def record_ingest_failure(self, *, reason: str) -> None:
        """Called by readings_publisher on 4xx/5xx (or network error)."""
        now = self.now()
        self.status = "error"
        self.last_ingest_at = now
        self.last_ingest_status = "error"
        self._ingest_times.append(now)
        self._prune_window(self._ingest_times, now)
        self._recent_ingests.append({
            "at": now.isoformat(),
            "status": "error",
            "reason": reason,
        })

    @property
    def ingest_count_24h(self) -> int:
        self._prune_window(self._ingest_times, self.now())
        return len(self._ingest_times)

    def recent_ingests(self) -> Iterable[dict[str, Any]]:
        return iter(self._recent_ingests)

    # ── command path ───────────────────────────────────────────────────

    def record_command(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        result: dict[str, Any] | None,
        success: bool,
    ) -> None:
        """Called by command_poller after dispatching a command. `result`
        is whatever the executor returned (None when rejected before
        dispatch)."""
        now = self.now()
        self.last_command_at = now
        self.last_command_kind = kind
        self._command_times.append(now)
        self._prune_window(self._command_times, now)
        self._recent_commands.append({
            "at": now.isoformat(),
            "kind": kind,
            "success": success,
            "payload": dict(payload),
            "result": dict(result) if isinstance(result, dict) else result,
        })

    @property
    def command_count_24h(self) -> int:
        self._prune_window(self._command_times, self.now())
        return len(self._command_times)

    def recent_commands(self) -> Iterable[dict[str, Any]]:
        return iter(self._recent_commands)

    # ── internal ───────────────────────────────────────────────────────

    @staticmethod
    def _prune_window(times: deque[datetime], now: datetime) -> None:
        """Drop entries older than 24h. Mutates in place."""
        cutoff = now - _COUNTER_WINDOW
        while times and times[0] < cutoff:
            times.popleft()
