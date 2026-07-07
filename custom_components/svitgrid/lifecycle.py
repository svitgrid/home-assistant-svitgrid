"""Shared device-lifecycle state (deprovisioning reaction)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACTIVE = "active"
PAUSED = "paused"
DEPROVISIONED = "deprovisioned"


@dataclass
class LifecycleState:
    state: str = ACTIVE
    reason: str | None = None
    since: str | None = None
    activity: Any = None  # optional ActivityTracker mirror

    def __post_init__(self) -> None:
        # C1: mirror a non-active SEEDED state into the activity tracker so
        # that after a restart with persisted deprovisioned/paused the status
        # sensor and binary_sensor reflect the real state immediately.
        if self.activity is not None and self.state != ACTIVE:
            self.activity.set_lifecycle(self.state, self.reason)

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    def _set(self, state: str, reason: str | None, now_iso: str) -> None:
        # C4: deprovisioned is a terminal state; prevent any downgrade.
        if self.state == DEPROVISIONED and state != DEPROVISIONED:
            return
        if self.state == state and self.reason == reason:
            return
        self.state = state
        self.reason = reason
        self.since = now_iso
        if self.activity is not None:
            self.activity.set_lifecycle(state, reason)

    def deprovision(self, reason: str | None, now_iso: str) -> None:
        self._set(DEPROVISIONED, reason, now_iso)

    def pause(self, reason: str | None, now_iso: str) -> None:
        self._set(PAUSED, reason, now_iso)
