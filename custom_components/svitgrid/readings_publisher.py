"""Readings publisher loop: builds a payload from current HA entity states
and CAPTURES it into the local SQLite store (capture-then-drain). A separate
sender (reading_sender.py) drains the store to the cloud and owns the adaptive
cadence via a shared `Cadence` holder. The publisher reads its produce
interval from `cadence.interval_s` (clamped) — it no longer talks to the cloud.
Matches the edge connector + mobile harvester: 60s during active sessions,
300s during idle.

We read state on-demand rather than maintaining a state_changed
subscription — simpler and sufficient at these cadences."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import CORE_PAYLOAD_FIELDS, READING_SOURCE

if TYPE_CHECKING:
    from .reading_sender import Cadence
    from .reading_store import ReadingStore

# Sane bounds for the server-driven cadence. Floor prevents a misbehaving
# server from spinning us in a 1ms tight loop; ceiling prevents the same
# server from silently parking us for hours on end.
_INTERVAL_FLOOR_S = 10
_INTERVAL_CEILING_S = 30 * 60
# Default when the response is missing ingestIntervalMs (old server, network
# error). 60s = active-session cadence; safer than the legacy 10s and never
# slower than idle.
_DEFAULT_INTERVAL_S = 60

# Aggregation kicks in when the server-requested interval is at least this
# long. Below this, single-snapshot pushes (T10a behavior) — the round-trip
# cost outweighs the data-quality benefit at fast cadences.
_AGGREGATION_THRESHOLD_S = 120
# Inside the aggregation branch, samples are collected this often.
_SAMPLE_TICK_S = 60

# Fields that are identity / metadata rather than numeric measurements.
# Aggregator takes the LAST sample's value for these instead of averaging.
_NON_NUMERIC_FIELDS = frozenset({"inverterId", "timestamp", "source"})

_LOGGER = logging.getLogger(__name__)

# States that mean "no usable value" and the field should be omitted entirely.
_UNAVAILABLE_STATES = {"unavailable", "unknown", "none", None}

# The entity_map / UI use pv1Power..pv4Power (MAPPABLE_FIELDS labels), but the
# Svitgrid API ingest schema's canonical per-string keys are pvPower1..pvPower4
# (same names the mobile harvester + edge firmware send). Translate on the way
# out so the server stores the per-string breakdown instead of stripping it.
_PV_STRING_API_NAMES = {
    "pv1Power": "pvPower1",
    "pv2Power": "pvPower2",
    "pv3Power": "pvPower3",
    "pv4Power": "pvPower4",
}


def build_reading_payload(
    *, hass: HomeAssistant, inverter_id: str, entity_map: dict[str, str]
) -> dict[str, Any]:
    """Build a single reading payload. Fields whose mapped entity is
    missing / unavailable / non-numeric are omitted (not sent as null or 0).
    """
    payload: dict[str, Any] = {
        "inverterId": inverter_id,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": READING_SOURCE,
    }
    for field, entity_id in entity_map.items():
        state = hass.states.get(entity_id)
        if state is None:
            continue
        raw = state.state
        if raw in _UNAVAILABLE_STATES or not isinstance(raw, str):
            continue
        try:
            payload[field] = float(raw)
        except (TypeError, ValueError):
            continue
    # Aggregate pvPower from the per-MPPT values the server requires.
    # (Server schema requires `pvPower` as a top-level scalar.)
    pv_total = 0.0
    has_any_pv = False
    for pv_field in ("pv1Power", "pv2Power", "pv3Power", "pv4Power"):
        if pv_field in payload:
            pv_total += payload[pv_field]
            has_any_pv = True
    if has_any_pv:
        payload["pvPower"] = pv_total
    # Emit the per-string fields under the server's canonical names
    # (pvPower1..pvPower4), matching the mobile harvester
    # (apps/mobile/.../upload_payload.dart) and edge firmware
    # (devices/.../cloud_uploader.c). The entity_map / UI keep the
    # pv1Power..pv4Power keys (MAPPABLE_FIELDS labels), but the API ingest
    # schema only recognizes pvPowerN — sending pvN silently strips the
    # per-string breakdown (total survives, strings show 0 in the app).
    for internal, api_name in _PV_STRING_API_NAMES.items():
        if internal in payload:
            payload[api_name] = payload.pop(internal)
    return payload


def gate_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Finalize a built payload and report whether it's safe to POST.

    1. Default `pvPower` to 0.0 when no PV-string value produced one, so
       battery-only / no-solar systems satisfy the API's required `pvPower`
       instead of being rejected forever.
    2. Return the sorted list of CORE_PAYLOAD_FIELDS still missing. An empty
       list means the payload is complete enough to send.

    Mutates and returns `payload` (caller passes a fresh dict per tick)."""
    if "pvPower" not in payload:
        payload["pvPower"] = 0.0
    missing = sorted(f for f in CORE_PAYLOAD_FIELDS if f not in payload)
    return payload, missing


def _aggregate_samples(
    samples: list[dict[str, Any]], period_s: int
) -> dict[str, Any]:
    """Combine N reading payloads into one aggregated payload.

    Numeric fields are averaged across the samples that contain them
    (missing values don't dilute the mean — each field is averaged over
    its own count). Identity fields (inverterId, timestamp, source) are
    taken from the LAST sample. sampleCount + periodSec are added so
    the server can interpret this as an aggregate window."""
    if not samples:
        # Defensive — caller should never pass empty. Return minimal stub.
        return {"sampleCount": 0, "periodSec": period_s}

    # Collect per-field sums + counts.
    field_sums: dict[str, float] = {}
    field_counts: dict[str, int] = {}
    for sample in samples:
        for key, value in sample.items():
            if key in _NON_NUMERIC_FIELDS:
                continue
            if isinstance(value, (int, float)):
                field_sums[key] = field_sums.get(key, 0.0) + float(value)
                field_counts[key] = field_counts.get(key, 0) + 1

    agg: dict[str, Any] = {}
    # Identity fields from the most recent sample.
    last = samples[-1]
    for key in _NON_NUMERIC_FIELDS:
        if key in last:
            agg[key] = last[key]

    # Numeric fields averaged.
    for key, total in field_sums.items():
        agg[key] = total / field_counts[key]

    agg["sampleCount"] = len(samples)
    agg["periodSec"] = period_s
    return agg


def _clamp_interval(seconds: float) -> float:
    """Clamp a produce interval to [floor, ceiling]. The cadence value comes
    from the shared `Cadence` holder (updated by the sender); clamping here
    keeps a misbehaving cadence value from freezing or tight-looping us."""
    if seconds < _INTERVAL_FLOOR_S:
        return float(_INTERVAL_FLOOR_S)
    if seconds > _INTERVAL_CEILING_S:
        return float(_INTERVAL_CEILING_S)
    return float(seconds)


_SUMMARY_FIELDS = ("pvPower", "loadPower", "batterySoc", "gridPower", "batteryPower")


def _summary_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Pick the 5 headline fields for the ActivityTracker recent-events buffer.
    Avoids storing the whole payload (mostly numeric noise on the device page)."""
    return {k: payload[k] for k in _SUMMARY_FIELDS if k in payload}


async def run_loop(
    *,
    hass: HomeAssistant,
    store: "ReadingStore",
    cadence: "Cadence",
    inverter_id: str,
    entity_map: dict[str, str],
    activity: Any = None,  # ActivityTracker; None acceptable for older callers
    lifecycle: Any = None,  # LifecycleState; None = no lifecycle gating
) -> None:
    """Long-running coroutine — capture-then-drain producer.

    Each produced reading is appended to the local `store`; the separate
    sender drains it to the cloud. The produce interval comes from the shared
    `cadence.interval_s` (clamped), which the sender updates from the server's
    ingestIntervalMs.

    Two cadence modes, decided by the (clamped) cadence value:

    - Fast (<120s): capture a single snapshot, then sleep the full interval.
      Matches active sessions / pending commands.
    - Idle (>=120s): sample every 60s for the duration, then capture ONE
      aggregated payload with sampleCount + periodSec. Better data (true
      period averages, not single-moment snapshots).

    Exits when `hass.is_stopping` becomes True."""
    _LOGGER.info(
        "Readings publisher started (capture-then-drain; "
        "floor=%ss, ceiling=%ss, aggregation>=%ss)",
        _INTERVAL_FLOOR_S, _INTERVAL_CEILING_S, _AGGREGATION_THRESHOLD_S,
    )
    # Cadence carries across iterations. Initial value = clamped holder value.
    next_sleep_s = _clamp_interval(float(cadence.interval_s))

    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        try:
            if next_sleep_s >= _AGGREGATION_THRESHOLD_S:
                # T10b: idle path — collect N samples then push aggregated.
                samples: list[dict[str, Any]] = []
                elapsed = 0
                while elapsed < next_sleep_s and not hass.is_stopping:
                    samples.append(build_reading_payload(
                        hass=hass, inverter_id=inverter_id, entity_map=entity_map,
                    ))
                    await asyncio.sleep(_SAMPLE_TICK_S)
                    elapsed += _SAMPLE_TICK_S
                if samples:
                    aggregated = _aggregate_samples(samples, period_s=elapsed)
                    aggregated, missing = gate_payload(aggregated)
                    if missing:
                        _LOGGER.warning(
                            "Skipping aggregated ingest — incomplete reading; "
                            "missing %s. Check these sensors in HA.",
                            missing,
                        )
                        if activity is not None:
                            activity.record_ingest_skipped(
                                missing_fields=missing,
                                entities={f: entity_map.get(f) for f in missing},
                            )
                        continue
                    await store.append(aggregated)
                    next_sleep_s = _clamp_interval(float(cadence.interval_s))
                    if activity is not None:
                        activity.record_ingest_success(
                            sample_count=len(samples),
                            period_sec=elapsed,
                            summary=_summary_of(aggregated),
                        )
            else:
                # Active path — single snapshot, gate, then sleep.
                payload = build_reading_payload(
                    hass=hass, inverter_id=inverter_id, entity_map=entity_map,
                )
                payload, missing = gate_payload(payload)
                if missing:
                    _LOGGER.warning(
                        "Skipping ingest — incomplete reading; missing %s. "
                        "Check these sensors exist and are available in HA.",
                        missing,
                    )
                    if activity is not None:
                        activity.record_ingest_skipped(
                            missing_fields=missing,
                            entities={f: entity_map.get(f) for f in missing},
                        )
                    await asyncio.sleep(next_sleep_s)
                    continue
                await store.append(payload)
                next_sleep_s = _clamp_interval(float(cadence.interval_s))
                if activity is not None:
                    activity.record_ingest_success(
                        sample_count=1,
                        period_sec=int(next_sleep_s),
                        summary=_summary_of(payload),
                    )
                await asyncio.sleep(next_sleep_s)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Readings publish failed; will retry next tick")
            if activity is not None:
                activity.record_ingest_failure(reason=str(exc) or type(exc).__name__)
            # On error, fall back to default cadence to avoid tight retry
            # loops or hour-long parks.
            next_sleep_s = float(_DEFAULT_INTERVAL_S)
            await asyncio.sleep(next_sleep_s)
    _LOGGER.info("Readings publisher stopped")
