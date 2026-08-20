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
from .link import TransactionFailed

_LOGGER = logging.getLogger(__name__)


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


async def run_eybond_harvest_loop(
    *,
    hass,
    link,
    reader,
    store,
    cadence,
    inverter_id: str,
    lifecycle=None,
    sleep=asyncio.sleep,
) -> None:
    """Poll the collector on the shared cadence until shutdown.

    Mirrors `harvest.engine.run_direct_harvest_loop`: the interval is read
    from the shared cadence holder each tick so the sender can adjust it, and
    any exception falls back to the default interval rather than spinning.

    `sleep` is injectable so tests can drive many ticks quickly. The cadence
    clamp has a 5 s floor, which is right in production and far too slow for a
    test; overriding the WAIT keeps the clamp itself under test.
    """
    _LOGGER.info("EyBond harvest loop started for inverter %s", inverter_id)
    was_connected = False
    unknown_platform_logged = False

    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        next_sleep_s = _clamp_interval(float(cadence.interval_s))
        try:
            connected = link.collector_connected
            if connected and not was_connected:
                # A fresh session may be a different inverter entirely.
                reader.invalidate()
                unknown_platform_logged = False
                _LOGGER.info("%s: collector connected, re-identifying", inverter_id)
            was_connected = connected

            if not connected:
                _LOGGER.debug("%s: no collector connected, skipping tick", inverter_id)
            else:
                payload = await poll_once(reader=reader, inverter_id=inverter_id, store=store)
                if payload is not None:
                    _LOGGER.debug("%s: reading appended", inverter_id)
        except UnknownPlatform as err:
            # Loud once, then quiet. It cannot resolve itself without either a
            # capture of this platform or a different device, so repeating it
            # every tick would bury everything else in the log.
            if not unknown_platform_logged:
                _LOGGER.error("%s: refusing to publish. %s", inverter_id, err)
                unknown_platform_logged = True
        except TransactionFailed as err:
            _LOGGER.debug("%s: poll failed: %s", inverter_id, err)
        except Exception:
            _LOGGER.exception("%s: unexpected harvest error", inverter_id)
            next_sleep_s = _DEFAULT_INTERVAL_S

        await sleep(next_sleep_s)
