"""One mapping from the cloud's description of an inverter to the entry's.

The API describes an inverter in camelCase in three different places — the
`/finalize` response, its `inverters` array, and an `add_inverter` command
payload — and the config entry stores it in snake_case. Written out at each of
those sites, the three copies drift, and a key that stops being translated
fails in the quiet direction: the inverter is stored, looks configured, and is
polled with a field missing.
"""

from __future__ import annotations

import re
from typing import Any

# The command hub defaults, applied to every inverter that does not carry its
# own. Unchanged from the values the pairing flow has always written.
_DEFAULT_COMMAND_CONFIG = {"hub_name": "solarman", "slave_id": 1, "battery_voltage": 52.8}

_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])([A-Z])")


def _snake_case(key: str) -> str:
    return _CAMEL_HUMP.sub(r"_\1", key).lower()


def harvest_config_from_api(harvest_config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a cloud `harvestConfig` with snake_case keys.

    The API sends `slaveId`, `modelId` and `loggerSerial`; the setup loop, the
    transport, the write executor and settings sync read `slave_id`, `model_id`
    and `logger_serial`. Stored unconverted, the inverter looks configured and
    its spec load raises a `KeyError` that setup swallows, so it is polled by
    nothing (issue #5).

    Every key is converted, not a fixed list, so a field the API adds later
    arrives in the spelling the add-on reads. Keys already in snake_case pass
    through, which makes the conversion safe to repeat on a stored entry. When
    a dict holds both spellings of one key, the snake_case value wins: it was
    written by this add-on, after the camelCase copy was stored.
    """
    converted: dict[str, Any] = {}
    for key, value in harvest_config.items():
        snake = _snake_case(key)
        if snake != key and snake in harvest_config:
            continue
        converted[snake] = value
    return converted


def inverter_entry_from_api(desc: dict[str, Any]) -> dict[str, Any]:
    """Translate one cloud inverter description into an entry `inverters` item.

    `inverterId` is required; everything else is optional, because a relay
    inverter has no `harvestConfig` and a manual one has no `presetId`, and
    neither absence is an error.
    """
    entry: dict[str, Any] = {
        "inverter_id": desc["inverterId"],
        "entity_map": desc.get("entityMap") or {},
        "command_recipes": desc.get("commands") or [],
        "command_config": dict(_DEFAULT_COMMAND_CONFIG),
        "brand": desc.get("brand"),
        "model": desc.get("model"),
        "phases": desc.get("phases"),
        "has_battery": desc.get("hasBattery"),
        "pv_strings": desc.get("pvStrings"),
        "preset_id": desc.get("presetId"),
    }
    # Only when the cloud sent one: adding `harvest_config: None` to a relay
    # inverter would make it look like a direct-Modbus unit with no address,
    # which is the shape the setup loop treats as a broken config.
    harvest = desc.get("harvestConfig")
    if harvest:
        entry["harvest_config"] = harvest_config_from_api(harvest)
    return entry


def inverters_from_finalize(payload: dict[str, Any], *, fallback_id: str) -> list[dict[str, Any]]:
    """Every inverter a `/finalize` response describes.

    The `inverters` array wins when it holds anything: it is the only shape
    that can describe a second unit, and an API that sends it has itemised the
    whole station. The flat fields are the fallback, and they are what an API
    deployed before the array returns — so an EMPTY array alongside populated
    flat fields is read as "could not itemise", not as "no inverters". Reading
    it literally would leave the publisher with nothing to publish while the
    entry looked perfectly healthy.
    """
    described = payload.get("inverters")
    if described:
        return [inverter_entry_from_api(d) for d in described]
    return [
        inverter_entry_from_api(
            {
                "inverterId": fallback_id,
                "entityMap": payload.get("entityMap"),
                "commands": payload.get("commands"),
                "brand": payload.get("brand"),
                "model": payload.get("model"),
                "phases": payload.get("phases"),
                "hasBattery": payload.get("hasBattery"),
                "pvStrings": payload.get("pvStrings"),
                "presetId": payload.get("presetId"),
            }
        )
    ]
