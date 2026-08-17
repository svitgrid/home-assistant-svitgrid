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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

_RECENT_BUFFER_SIZE = 10
_COUNTER_WINDOW = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class ActivityTracker:
    """Mutable, single-writer shared object. readings_publisher and
    command_poller both call record_* methods; sensor.py reads."""

    now: Callable[[], datetime] = _utc_now

    # Most recent outcome — used as the `sensor.svitgrid_status` value.
    # Internal field; the `status` property applies lifecycle precedence.
    _status: str = "idle"

    # Lifecycle state set by LifecycleState mirror (Task 2).
    lifecycle_state: str = "active"
    lifecycle_reason: str | None = None

    # Whether the cloud has APPROVED this install's signing key. Defaults True
    # and is only ever set False by an explicit `trustedKeyStatus: "pending"`
    # from /finalize — fail-OPEN, because an API that doesn't send the field at
    # all (older deployment) must not put every healthy household into a false
    # "waiting for approval" state. A pending key does not stop telemetry (that
    # rides the API key); it makes the server refuse every command ACK.
    signing_key_approved: bool = True

    # Set when a direct-harvest inverter's register spec cannot be executed
    # (unknown builtin / malformed document / never fetched). Sticky until
    # cleared, because the symptom — no readings at all, for ever — never
    # produces an ingest event of its own to overwrite the status with.
    spec_problem: str | None = None

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

    # ── lifecycle mirror ───────────────────────────────────────────────

    @property
    def status(self) -> str:
        """Return lifecycle state when not active; otherwise ingest status."""
        if self.lifecycle_state != "active":
            return self.lifecycle_state
        return self._status

    def set_lifecycle(self, state: str, reason: str | None) -> None:
        """Mirror lifecycle changes from LifecycleState (Task 2)."""
        self.lifecycle_state = state
        self.lifecycle_reason = reason

    def set_signing_key_approved(self, approved: bool) -> None:
        """Set from /finalize's `trustedKeyStatus`, and flipped back to True by
        command_poller when the `add_trusted_key` carrying THIS install's own
        key arrives — which is exactly what the server sends on approval, so
        the warning clears itself with no polling and no extra reads."""
        self.signing_key_approved = approved

    # ── register-spec health ───────────────────────────────────────────

    def record_spec_problem(self, *, model_id: str, detail: str) -> None:
        """Called by harvest.spec_health when a spec cannot be executed."""
        self.spec_problem = f"{model_id}: {detail}"

    def clear_spec_problem(self) -> None:
        self.spec_problem = None

    # ── ingest path ────────────────────────────────────────────────────

    def record_ingest_success(
        self,
        *,
        sample_count: int,
        period_sec: int,
        summary: dict[str, Any],
        unresolved: dict[str, str] | None = None,
    ) -> None:
        """Called by readings_publisher after a 2xx ack from /ingest/reading.
        `summary` should contain a few headline fields (pvPower, loadPower,
        batterySoc) for at-a-glance status — full payload not stored.

        `unresolved` maps each MAPPED field that produced no value this tick
        to its entity id. The reading still went out, so this is not a
        failure — but a mapped-and-dead sensor otherwise just reads as a flat
        0 in the app with nothing anywhere naming it, so it is carried onto
        the Diagnostics line."""
        now = self.now()
        self._status = "ok"
        self.last_ingest_at = now
        self.last_ingest_status = "ok"
        self._ingest_times.append(now)
        self._prune_window(self._ingest_times, now)
        self._recent_ingests.append(
            {
                "at": now.isoformat(),
                "status": "ok",
                "sample_count": sample_count,
                "period_sec": period_sec,
                "summary": dict(summary),
                "unresolved": dict(unresolved or {}),
            }
        )

    def record_ingest_failure(self, *, reason: str) -> None:
        """Called by readings_publisher on 4xx/5xx (or network error)."""
        now = self.now()
        self._status = "error"
        self.last_ingest_at = now
        self.last_ingest_status = "error"
        self._ingest_times.append(now)
        self._prune_window(self._ingest_times, now)
        self._recent_ingests.append(
            {
                "at": now.isoformat(),
                "status": "error",
                "reason": reason,
            }
        )

    def record_ingest_skipped(
        self,
        *,
        missing_fields: list[str],
        entities: dict[str, str | None],
    ) -> None:
        """Called by readings_publisher when a reading is NOT sent because
        required fields are missing/unavailable. Distinct from a network
        failure — no POST was attempted, so it does not touch the 24h counter.
        `entities` maps each missing field to its mapped entity id (or None
        when the field was never mapped) so the user can find the culprit."""
        now = self.now()
        self._status = "waiting"
        self.last_ingest_at = now
        self.last_ingest_status = "skipped"
        self._recent_ingests.append(
            {
                "at": now.isoformat(),
                "status": "skipped",
                "missing_fields": list(missing_fields),
                "entities": dict(entities),
            }
        )

    @property
    def ingest_count_24h(self) -> int:
        self._prune_window(self._ingest_times, self.now())
        return len(self._ingest_times)

    def recent_ingests(self) -> Iterable[dict[str, Any]]:
        return iter(self._recent_ingests)

    def diagnostics_line(self) -> str:
        """A short (<=255 char) human status for the diagnostics sensor state."""
        if self.lifecycle_state == "deprovisioned":
            return "Device removed from its household — re-pair to resume."
        if self.lifecycle_state == "paused":
            return f"Paused by operator: {self.lifecycle_reason or 'disabled'}"[:255]
        # Ranked above everything below: a spec the decoder cannot execute means
        # that inverter publishes NOTHING, ever — worse than an incomplete
        # reading, and unlike every other failure it never produces an ingest
        # event of its own to move the line off "idle".
        if self.spec_problem:
            return f"register spec problem — {self.spec_problem}"[:255]
        if self.last_ingest_status == "skipped":
            recent = self._recent_ingests
            missing = recent[-1].get("missing_fields", []) if recent else []
            line = f"waiting — incomplete reading; missing: {', '.join(missing)}"
            return line[:255]
        # Ranked BELOW an incomplete reading on purpose: a gated reading means
        # no data at all, which is more urgent than control commands being
        # refused.
        if not self.signing_key_approved:
            return (
                "waiting for approval — open the Svitgrid app -> Household -> "
                "Your devices and approve this integration"
            )[:255]
        if self.last_ingest_status == "ok":
            recent = self._recent_ingests
            unresolved = recent[-1].get("unresolved", {}) if recent else {}
            if not unresolved:
                return "ok"
            named = ", ".join(f"{field} ({eid})" for field, eid in sorted(unresolved.items()))
            return f"ok — but no value from mapped sensor(s): {named}"[:255]
        if self.last_ingest_status == "error":
            recent = self._recent_ingests
            reason = recent[-1].get("reason", "") if recent else ""
            return f"error: {reason}"[:255]
        return "idle"

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
        self._recent_commands.append(
            {
                "at": now.isoformat(),
                "kind": kind,
                "success": success,
                "payload": dict(payload),
                "result": dict(result) if isinstance(result, dict) else result,
            }
        )

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
