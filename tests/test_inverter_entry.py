"""The cloud's `harvestConfig` is camelCase; every reader of the entry is not.

The direct-harvest loop, the write executor, the transport and settings sync
all read `harvest_config["model_id"]`, `["slave_id"]` and `["logger_serial"]`.
A camelCase dict stored verbatim looks configured, and then the spec load
raises a `KeyError` that setup swallows: the loop idles forever and no reading
ever reaches the cloud (issue #5).
"""

from __future__ import annotations

from custom_components.svitgrid.inverter_entry import (
    harvest_config_from_api,
    inverter_entry_from_api,
    inverters_from_finalize,
)

_CAMEL = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.50",
    "port": 8899,
    "slaveId": 1,
    "modelId": "deye_sg04lp3",
    "loggerSerial": "1234567890",
}
_SNAKE = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.50",
    "port": 8899,
    "slave_id": 1,
    "model_id": "deye_sg04lp3",
    "logger_serial": "1234567890",
}


def test_the_cloud_harvest_config_is_stored_snake_cased() -> None:
    entry = inverter_entry_from_api({"inverterId": "ha-1", "harvestConfig": dict(_CAMEL)})

    assert entry["harvest_config"] == _SNAKE


def test_every_inverter_in_a_finalize_array_is_snake_cased() -> None:
    invs = inverters_from_finalize(
        {"inverters": [{"inverterId": "ha-1", "harvestConfig": dict(_CAMEL)}]},
        fallback_id="unused",
    )

    assert invs[0]["harvest_config"]["model_id"] == "deye_sg04lp3"
    assert "modelId" not in invs[0]["harvest_config"]


def test_a_relay_inverter_still_carries_no_harvest_config() -> None:
    assert "harvest_config" not in inverter_entry_from_api({"inverterId": "ha-1"})


def test_the_converter_is_idempotent_on_a_snake_cased_config() -> None:
    assert harvest_config_from_api(dict(_SNAKE)) == _SNAKE


def test_a_mixed_config_prefers_the_snake_cased_value() -> None:
    """A dict that already carries both spellings was touched by a snake-case
    writer after the camelCase copy was stored. That writer is the newer one."""
    mixed = {**_CAMEL, "slave_id": 3}

    assert harvest_config_from_api(mixed)["slave_id"] == 3


def test_the_converter_does_not_mutate_its_input() -> None:
    src = dict(_CAMEL)
    harvest_config_from_api(src)

    assert src == _CAMEL


def test_keys_it_does_not_know_are_converted_too() -> None:
    """The list of fields is the API's to grow. A field added there must not
    need a matching edit here to arrive in the shape the add-on reads."""
    assert harvest_config_from_api({"listenPort": 8899})["listen_port"] == 8899
