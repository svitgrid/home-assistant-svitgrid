"""Harvest loop for an Anenji behind a SmartESS/EyBond collector.

Feeds the EXISTING pipeline: `assemble_payload` -> `gate_payload` ->
`store.append`, and the reading sender drains the store to Svitgrid. Nothing
about storage, cadence, gating, or sending is reimplemented here.

── Why this is not a `harvest.transport` protocol ────────────────────────
`harvest/transport.py` dispatches on `spec.protocol` and reads the register
list from a `RegisterSpec` fetched from Svitgrid, keyed by **model id**. That
shape cannot express this family, because the register map here is chosen by
a value the DEVICE reports at runtime — protocol number, register 184 — not
by the model a user picked during onboarding. Anenji ships at least two
different register maps under one brand, so a model-keyed spec would decode
some units with the wrong one and publish plausible, wrong numbers.

If the spec system later gains runtime dispatch, this loop becomes a thin
adapter over it. Until then the map lives in `register_map.py`, where its
provenance and per-field confidence are recorded beside the addresses.

── Collector reconnects invalidate identity ──────────────────────────────
The collector may come back as a different inverter; a customer can swap one
without telling us. Every observed reconnect clears the cached identity, so
the map is re-resolved before the next reading is trusted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..readings_publisher import (
    _DEFAULT_INTERVAL_S,
    _clamp_interval,
    assemble_payload,
    gate_payload,
)
from .identity import UnknownPlatform
from .reader import EybondAtReader
from .session import TransactionFailed

_LOGGER = logging.getLogger(__name__)

# Ceiling on how long to block waiting for a collector. The wait ends as soon
# as one is identified, so this only bounds the case where none ever arrives.
#
# Why this exists: while no collector is connected there is nothing to read,
# so the poll cadence does not apply -- we are waiting for an EVENT, not
# pacing a device. Sleeping the cadence here made a fresh install wait five
# minutes for its first reading. Measured 2026-08-20: the loop's first tick
# ran 0.6 s before the collector connected, and the reading then waited out a
# full 300 s cadence while the user watched "Waiting for data".
_WAITING_FOR_COLLECTOR_S = 30.0


async def poll_once(*, reader, inverter_id: str, store) -> dict | None:
    """One reading: poll, assemble, gate, append.

    Returns the appended payload, or None when the reading was gated. A gated
    reading is normal rather than an error — an incomplete block yields no
    values at all, and the gate is what stops a half-reading reaching the API.
    """
    reading = await reader.read()
    if not reading.complete:
        _LOGGER.debug(
            "%s: reading incomplete, missing blocks %s", inverter_id, reading.missing_blocks
        )
    values: dict[str, Any] = dict(reading.values)
    payload = assemble_payload(inverter_id=inverter_id, fields=values)
    payload, missing = gate_payload(payload)
    if missing:
        _LOGGER.debug("%s: gated, missing required fields %s", inverter_id, missing)
        return None
    await store.append(payload)
    return payload


def first_poll_retry_s(*, attempt: int, cadence_s: float) -> float:
    """How long to wait before retrying a poll that has never yet succeeded.

    The steady-state cadence is 300 s, which is right once data is flowing and
    badly wrong before it. A single transient timeout on the FIRST poll used to
    cost five minutes on the "waiting for data" screen a new user is watching --
    measured 2026-08-21: bound 17:08:36, poll failed 17:08:45, next attempt
    17:13:45.

    So: seconds at first, doubling, and never slower than the normal cadence --
    converging on it rather than overshooting, and never making a short cadence
    longer than it already is.
    """
    return min(5.0 * (2 ** max(0, attempt - 1)), cadence_s)


async def run_eybond_harvest_loop(
    *,
    hass,
    hub,
    inverter_serial: str | None,
    store,
    cadence,
    inverter_id: str,
    lifecycle=None,
    activity=None,
    sleep=asyncio.sleep,
    reader_factory=None,
) -> None:
    """Poll the collector on the shared cadence until shutdown.

    Mirrors `harvest.engine.run_direct_harvest_loop`: the interval is read
    from the shared cadence holder each tick so the sender can adjust it, and
    any exception falls back to the default interval rather than spinning.

    `sleep` is injectable so tests can drive many ticks quickly. The cadence
    clamp has a 5 s floor, which is right in production and far too slow for a
    test; overriding the WAIT keeps the clamp itself under test.
    """
    _LOGGER.info(
        "EyBond harvest loop started for inverter %s (serial %s)",
        inverter_id,
        inverter_serial or "unset",
    )
    make_reader = reader_factory or EybondAtReader
    current_session = None
    reader = None
    unknown_platform_logged = False
    # Until a reading lands, failures are retried fast; see first_poll_retry_s.
    first_reading_done = False
    failed_first_polls = 0

    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        next_sleep_s = _clamp_interval(float(cadence.interval_s))
        try:
            # Routed by the serial the COLLECTOR reports at register 186 --
            # never connection order, never IP. Order is whatever the
            # collectors do after a power cut, and a DHCP lease moves.
            session = hub.session_for(inverter_serial)
            if session is not current_session:
                # A different session is a different connection, and possibly
                # a different device. A fresh reader re-identifies rather than
                # carrying the previous unit's register map.
                current_session = session
                reader = make_reader(session) if session is not None else None
                unknown_platform_logged = False
                if session is not None:
                    _LOGGER.info("%s: bound to collector at %s", inverter_id, session.address)

            if reader is None:
                # Normal for an inverter that is switched off. Not an error.
                # Wait for one to ARRIVE rather than sleeping the cadence, so
                # the first reading lands with onboarding instead of minutes
                # after it.
                _LOGGER.debug(
                    "%s: serial %s not connected, waiting",
                    inverter_id,
                    inverter_serial or "unset",
                )
                if hasattr(hub, "wait_for_change"):
                    await hub.wait_for_change(min(next_sleep_s, _WAITING_FOR_COLLECTOR_S))
                    continue
                next_sleep_s = min(next_sleep_s, _WAITING_FOR_COLLECTOR_S)
            else:
                payload = await poll_once(reader=reader, inverter_id=inverter_id, store=store)
                if payload is not None:
                    _LOGGER.debug("%s: reading appended", inverter_id)
                    first_reading_done = True
                    failed_first_polls = 0
        except UnknownPlatform as err:
            # Loud once, then quiet. It cannot resolve itself without either a
            # capture of this platform or a different device, so repeating it
            # every tick would bury everything else in the log.
            if not unknown_platform_logged:
                _LOGGER.error("%s: refusing to publish. %s", inverter_id, err)
                unknown_platform_logged = True
            # An ERROR in the log is not something a user finds. This inverter
            # publishes NOTHING and cannot recover on its own, which is the
            # case `spec_problem` is ranked top of the diagnostics line for --
            # otherwise the owner sees a device that paired and then sat at
            # "idle" forever. Set every tick, not just the first: the sensor
            # is created after the loop starts, so a first-tick-only write
            # would be lost.
            if activity is not None:
                activity.spec_problem = str(err)
        except TransactionFailed as err:
            _LOGGER.debug("%s: poll failed: %s", inverter_id, err)
            # Before the FIRST reading, retry in seconds rather than sleeping
            # the steady-state cadence. One transient timeout otherwise puts
            # five minutes on the onboarding screen -- measured 2026-08-21.
            if not first_reading_done:
                failed_first_polls += 1
                next_sleep_s = first_poll_retry_s(
                    attempt=failed_first_polls, cadence_s=next_sleep_s
                )
                _LOGGER.debug(
                    "%s: no reading yet; retrying in %.0fs (attempt %d)",
                    inverter_id,
                    next_sleep_s,
                    failed_first_polls,
                )
        except Exception:
            _LOGGER.exception("%s: unexpected harvest error", inverter_id)
            next_sleep_s = _DEFAULT_INTERVAL_S

        await sleep(next_sleep_s)
