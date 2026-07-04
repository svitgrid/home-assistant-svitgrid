"""Battery-power sign convention for HA-sourced inverters.

The Home Assistant "Solarman" integration exposes battery power as
DISCHARGE-positive / CHARGE-negative — the inverse of Svitgrid's convention
(charge positive, discharge negative), which every other harvester path, the
cloud, and the mobile app use.

Two coordinated flips keep every consumer correct WITHOUT touching the server:

1. CAPTURE (readings_publisher.build_reading_payload): for solarman inverters
   we negate `batteryPower` so the local SQLite store — and therefore the
   branded panel's live card + Day/History charts, which read the store — hold
   Svitgrid's convention. Otherwise a charging battery (negative HA value)
   renders as "discharging".

2. SEND (reading_sender.drain_once): we re-invert `batteryPower` for the same
   inverters before upload, so the cloud keeps receiving the raw
   discharge-positive value that `services/api/src/services/process-reading.ts`
   already negates for `home_assistant_solarman`. The server contract is
   unchanged, so there is no cross-version transition window.

Convention key: an inverter uses the discharge-positive convention iff it was
configured from a Solarman preset. All such presets are seeded (see
`scripts/seed-ha-presets.cjs`) with ids like `deye-sg04lp3-solarman-v1` and
`protocolId: 'home_assistant_solarman'`; raw `home_assistant` presets
(e.g. `anenji-*`) and manual (preset-less) inverters never contain `solarman`.
"""

from __future__ import annotations

from typing import Any


def preset_is_discharge_positive(preset_id: str | None) -> bool:
    """True iff an inverter's preset uses the HA-Solarman discharge-positive
    battery convention (and therefore needs sign normalization)."""
    return bool(preset_id) and "solarman" in preset_id


def flip_battery_sign(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``payload`` with ``batteryPower`` negated.

    No-op (returns the input unchanged) when ``batteryPower`` is absent or not a
    real number — ``bool`` is rejected so a stray ``True``/``False`` never gets
    treated as 1/0.
    """
    bp = payload.get("batteryPower")
    if isinstance(bp, bool) or not isinstance(bp, (int, float)):
        return payload
    out = dict(payload)
    out["batteryPower"] = -bp
    return out
