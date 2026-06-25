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

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    def _set(self, state: str, reason: str | None, now_iso: str) -> None:
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
