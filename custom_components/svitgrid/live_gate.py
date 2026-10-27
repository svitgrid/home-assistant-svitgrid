"""Live reporting: when to send a reading, and how the server switches it on.

In Live mode the integration reads every `check_s` seconds and sends a reading
only when a power moved `delta_w` watts or the state of charge moved
`delta_soc_pct` percent since the last reading it SENT, or when `heartbeat_s`
seconds passed without a send. This is a port of the edge firmware
(`live_gate.c` and `live_cadence.c`), rule for rule, so a Home Assistant
install and an edge connector send on the same moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POWER_KEYS = ("pvPower", "gridPower", "loadPower", "batteryPower")
SOC_KEY = "batterySoc"


@dataclass(frozen=True)
class LiveParams:
    check_s: int = 5
    delta_w: float = 50
    delta_soc_pct: float = 1
    heartbeat_s: int = 30


@dataclass(frozen=True)
class LiveControl:
    mode: str = "interval"
    params: LiveParams = field(default_factory=LiveParams)
    interval_s: int | None = None

    @property
    def live(self) -> bool:
        return self.mode == "live"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _live_field(obj: dict, key: str, lo: int, hi: int, fallback: int) -> int:
    # Out of range falls back to the default, never clamps: a clamped value
    # would look like the server asked for it.
    v = _number(obj.get(key))
    if v is None or v < lo or v > hi:
        return fallback
    return int(v)


def parse_live_control(msg: dict, current: LiveControl) -> LiveControl:
    """Apply one cadence message (an ingest response or an MQTT config).

    A message with neither `ingestIntervalMs` nor `ingestMode` says nothing
    about the mode, so `current` is returned unchanged.
    """
    interval_s = current.interval_s
    interval_accepted = False
    v = _number(msg.get("ingestIntervalMs"))
    if v is not None and v > 0:
        interval_s = int(v / 1000)
        interval_accepted = True

    mode = msg.get("ingestMode")
    if isinstance(mode, str):
        live = mode == "live"
    elif interval_accepted:
        # An interval with no mode is an interval-mode message.
        live = False
    else:
        return current

    d = LiveParams()
    params = d
    obj = msg.get("live")
    if live and isinstance(obj, dict):
        params = LiveParams(
            check_s=_live_field(obj, "checkSec", 1, 60, d.check_s),
            delta_w=_live_field(obj, "deltaW", 1, 100000, int(d.delta_w)),
            delta_soc_pct=_live_field(obj, "deltaSocPct", 1, 100, int(d.delta_soc_pct)),
            heartbeat_s=_live_field(obj, "heartbeatSec", 5, 3600, d.heartbeat_s),
        )
    return LiveControl(mode="live" if live else "interval", params=params, interval_s=interval_s)


def _moved(cur: dict, last: dict, key: str, threshold: float) -> bool:
    a, b = _number(cur.get(key)), _number(last.get(key))
    # A key missing from either reading is not compared.
    return a is not None and b is not None and abs(a - b) >= threshold


class LiveGate:
    """Decides, reading by reading, whether a Live reading goes out."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._has_sent = False
        self._has_attempted = False
        self._last_attempt_s = 0.0
        self._last_attempt_failed = False
        self._last_sent: dict = {}
        self._last_sent_s = 0.0

    @property
    def has_attempted(self) -> bool:
        return self._has_attempted

    @property
    def last_attempt_s(self) -> float:
        return self._last_attempt_s

    def should_send(self, reading: dict, now_s: float, params: LiveParams) -> bool:
        hb = params.heartbeat_s
        if not self._has_sent:
            # A first send that keeps failing retries once per heartbeat.
            return not self._has_attempted or now_s - self._last_attempt_s >= hb
        if self._last_attempt_failed:
            return now_s - self._last_attempt_s >= hb
        clock_start = max(self._last_sent_s, self._last_attempt_s)
        if now_s - clock_start >= hb:
            return True
        last = self._last_sent
        if any(_moved(reading, last, k, params.delta_w) for k in POWER_KEYS):
            return True
        return _moved(reading, last, SOC_KEY, params.delta_soc_pct)

    def record_attempt(self, reading: dict, now_s: float, ok: bool) -> None:
        self._has_attempted = True
        self._last_attempt_s = now_s
        self._last_attempt_failed = not ok
        if ok:
            self._has_sent = True
            self._last_sent = dict(reading)
            self._last_sent_s = now_s
