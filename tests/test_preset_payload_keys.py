"""Every `payload.*` a preset recipe references must be a key the API actually sends.

Why this exists
---------------
Relay presets are DSL recipes evaluated against the command payload the mobile
app sends. If a recipe references `payload.foo` and the API's command schema has
no `foo`, the key never arrives, the recipe raises `DslEvalError: missing key`,
and the command dies — silently, from the user's point of view, because the app
had already reported the command as sent.

That is not hypothetical. On 2026-08-16 an audit found `set_battery_charge`
referencing `payload.chargePowerLimitW` while the API sends `powerLimit`, so the
charge-power-limit command had been dead on every relay household for as long as
the preset existed. Nothing detected it: the schema strips unknown keys rather
than rejecting them, so neither side ever errored.

`ACCEPTED_PAYLOAD_KEYS` mirrors `DirectCommandSchema.payload` in the monorepo
(`services/api/src/routes/inverters.ts`). It is duplicated here rather than
vendored because the two repos release independently; the test below is the
thing that makes the duplication safe to have.
"""

from __future__ import annotations

import glob
import os
import re

import yaml

# Mirrors DirectCommandSchema.payload — services/api/src/routes/inverters.ts:135.
# Add a key here only when it exists there.
ACCEPTED_PAYLOAD_KEYS = {
    "workMode",
    "solarSell",
    "slotIndex",
    "gridChargeSoc",
    "gridChargeEnabled",
    "chargeVoltage",
    "powerLimit",
    "slotStart",
    "slotEnd",
    "prevSlots",
}

# Keys a recipe references ON PURPOSE despite the API not sending them, so the
# command fails closed. Each entry must say why removing it would be worse than
# leaving it broken.
KNOWN_BROKEN = {
    # Register 248 is bit 0 = TOU/timer enable, bits 1-7 = the DAY-OF-WEEK mask.
    # `modbus.write_register` stamps the whole word and the relay transport has
    # no read capability, so making this key resolve would erase the user's
    # day-of-week schedule on every toggle. The mismatch IS the safety catch.
    # Real fix: persist the raw 248 word server-side and stamp a bit-preserving
    # value. See the comment block above `set_grid_charge_toggle` in the presets.
    "enabled",
}

PRESET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "presets")
_PAYLOAD_REF = re.compile(r"payload\.([A-Za-z0-9_]+)")


def _referenced_keys(path: str) -> set[str]:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    # Scan the parsed document, not the raw text, so a key mentioned only in a
    # comment cannot satisfy — or trip — this check.
    return set(_PAYLOAD_REF.findall(yaml.safe_dump(doc)))


def test_preset_dir_is_not_empty() -> None:
    # Guards the whole suite against silently passing on a bad path: an empty
    # glob would make every assertion below vacuous.
    assert glob.glob(os.path.join(PRESET_DIR, "*.yaml")), PRESET_DIR


def test_every_referenced_payload_key_is_one_the_api_sends() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(glob.glob(os.path.join(PRESET_DIR, "*.yaml"))):
        unknown = _referenced_keys(path) - ACCEPTED_PAYLOAD_KEYS - KNOWN_BROKEN
        if unknown:
            offenders[os.path.basename(path)] = unknown
    assert not offenders, (
        "preset recipes reference payload keys the API never sends, so these "
        "commands will fail closed at runtime: "
        f"{ {k: sorted(v) for k, v in offenders.items()} }. "
        "Either rename to a key in ACCEPTED_PAYLOAD_KEYS, or — if it must stay "
        "broken — add it to KNOWN_BROKEN with the reason."
    )


def test_known_broken_keys_are_still_actually_broken() -> None:
    # If the API ever starts sending a KNOWN_BROKEN key, the safety catch is
    # gone and the recipe becomes live. That must be a deliberate change, not a
    # surprise, so fail until someone removes it from KNOWN_BROKEN on purpose.
    live = KNOWN_BROKEN & ACCEPTED_PAYLOAD_KEYS
    assert not live, (
        f"{sorted(live)} is listed as deliberately broken but the API now sends "
        "it — the recipe is live. Confirm the write is bit-preserving before "
        "removing it from KNOWN_BROKEN."
    )
