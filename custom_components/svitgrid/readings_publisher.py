"""Readings publisher loop: builds a payload from current HA entity states
and POSTs it to /api/v1/ingest/reading. Sleep cadence is adaptive — the
server response carries `ingestIntervalMs` which the publisher uses to
size the next sleep. Matches the edge connector + mobile harvester:
60s during active sessions, 300s during idle.

We read state on-demand rather than maintaining a state_changed
subscription — simpler and sufficient at these cadences."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import DeviceStopped, SvitgridApiClient
from .const import READING_SOURCE, READINGS_INTERVAL_S

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


def _next_interval_s(response: dict[str, Any] | None) -> float:
    """Pick the next sleep duration based on the server's ingestIntervalMs.
    Falls back to default on missing field / null response; clamps to
    [floor, ceiling] to absorb pathological server values."""
    if response is None:
        return float(_DEFAULT_INTERVAL_S)
    raw_ms = response.get("ingestIntervalMs")
    if not isinstance(raw_ms, (int, float)):
        return float(_DEFAULT_INTERVAL_S)
    seconds = raw_ms / 1000.0
    if seconds < _INTERVAL_FLOOR_S:
        return float(_INTERVAL_FLOOR_S)
    if seconds > _INTERVAL_CEILING_S:
        return float(_INTERVAL_CEILING_S)
    return seconds


_SUMMARY_FIELDS = ("pvPower", "loadPower", "batterySoc", "gridPower", "batteryPower")


def _summary_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Pick the 5 headline fields for the ActivityTracker recent-events buffer.
    Avoids storing the whole payload (mostly numeric noise on the device page)."""
    return {k: payload[k] for k in _SUMMARY_FIELDS if k in payload}


async def run_loop(
    *,
    hass: HomeAssistant,
    api_client: SvitgridApiClient,
    api_key: str,
    inverter_id: str,
    entity_map: dict[str, str],
    # interval_s kept for backwards-compat with callers; ignored once the
    # server returns ingestIntervalMs.
    interval_s: int = READINGS_INTERVAL_S,
    activity: Any = None,  # ActivityTracker; None acceptable for older callers
) -> None:
    """Long-running coroutine.

    Two cadence modes, decided after each push by the server's
    ingestIntervalMs response:

    - Fast (<120s): push a single snapshot, then sleep the full interval.
      Matches active sessions / pending commands.
    - Idle (>=120s): sample every 60s for the duration, then push ONE
      aggregated payload with sampleCount + periodSec. Same Firestore
      write count as the fast path's per-snapshot approach, but with
      better data (true period averages, not single-moment snapshots).

    Exits when `hass.is_stopping` becomes True."""
    _LOGGER.info(
        "Readings publisher started (adaptive cadence; "
        "default=%ss, floor=%ss, ceiling=%ss, aggregation>=%ss)",
        _DEFAULT_INTERVAL_S, _INTERVAL_FLOOR_S, _INTERVAL_CEILING_S,
        _AGGREGATION_THRESHOLD_S,
    )
    # Cadence carries across iterations. Initial value = default until
    # we receive the first response.
    next_sleep_s = float(_DEFAULT_INTERVAL_S)

    while not hass.is_stopping:
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
                    response = await api_client.push_reading(
                        api_key=api_key, reading=aggregated,
                    )
                    next_sleep_s = _next_interval_s(response)
                    if activity is not None:
                        if response is not None:
                            activity.record_ingest_success(
                                sample_count=len(samples),
                                period_sec=elapsed,
                                summary=_summary_of(aggregated),
                            )
                        else:
                            activity.record_ingest_failure(
                                reason="push_reading returned no response",
                            )
            else:
                # T10a: active path — single snapshot, then sleep.
                payload = build_reading_payload(
                    hass=hass, inverter_id=inverter_id, entity_map=entity_map,
                )
                response = await api_client.push_reading(
                    api_key=api_key, reading=payload,
                )
                next_sleep_s = _next_interval_s(response)
                if activity is not None:
                    if response is not None:
                        activity.record_ingest_success(
                            sample_count=1,
                            period_sec=int(next_sleep_s),
                            summary=_summary_of(payload),
                        )
                    else:
                        activity.record_ingest_failure(
                            reason="push_reading returned no response",
                        )
                await asyncio.sleep(next_sleep_s)
        except DeviceStopped as e:
            _LOGGER.warning(
                "Readings publisher: server signaled stop (%s); stopping loop. "
                "Operator can re-enable the device and you can reload the "
                "integration to resume.",
                e.reason,
            )
            return
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Readings publish failed; will retry next tick")
            if activity is not None:
                activity.record_ingest_failure(reason=str(exc) or type(exc).__name__)
            # On error, fall back to default cadence to avoid tight retry
            # loops or hour-long parks.
            next_sleep_s = float(_DEFAULT_INTERVAL_S)
            await asyncio.sleep(next_sleep_s)
    _LOGGER.info("Readings publisher stopped")
