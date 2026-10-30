"""Task 49 (stage 2B): Home Assistant entities for an edge box's Zigbee
devices and battery packs, read-only, following the stored roster."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid import binary_sensor as binary_sensor_mod
from custom_components.svitgrid import sensor as sensor_mod
from custom_components.svitgrid.activity import ActivityTracker
from custom_components.svitgrid.bms_packs import BmsPackStore
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.edge_entities import EdgeEntitiesCoordinator
from custom_components.svitgrid.zigbee_devices import ZigbeeDeviceStore

FIXTURES = Path(__file__).parent / "fixtures"
BOX = "EC-1"
PLUG_IEEE = "a4:c1:38:11:11:46:ff:ff"
PLUG_ID = "zb-a4c138111146ffff"


def _bms_report() -> dict:
    report = json.loads((FIXTURES / "bms_report.json").read_text())
    report.pop("_comment", None)
    return report


async def _setup(hass, tmp_path):
    """An entry with one adopted box, its stores and the entity coordinator,
    and both platforms set up. Returns (stores, coordinator, added entities)."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={"inverters": []})
    entry.add_to_hass(hass)
    zigbee = ZigbeeDeviceStore(str(tmp_path / "zigbee_devices.db"))
    packs = BmsPackStore(str(tmp_path / "bms_packs.db"))
    coordinator = EdgeEntitiesCoordinator(hass, zigbee, packs)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "activity": ActivityTracker(),
        "entry_data": dict(entry.data),
        "edge_entities": coordinator,
    }
    added: dict[str, object] = {}

    def _capture(entities, *a, **k):
        for e in entities:
            added[e.unique_id] = e

    await coordinator.async_refresh()
    await sensor_mod.async_setup_entry(hass, entry, _capture)
    await binary_sensor_mod.async_setup_entry(hass, entry, _capture)
    return entry, zigbee, packs, coordinator, added


async def _add_plug(zigbee: ZigbeeDeviceStore) -> None:
    await zigbee.async_add(BOX, PLUG_IEEE, "0x1234", 1)
    await zigbee.async_update(
        PLUG_ID,
        {"on": True, "caps": 3, "model": "S60ZBTPF", "manufacturer": "SONOFF", "name": "Kettle"},
    )
    await zigbee.async_store_socket_readings(
        BOX,
        "2026-10-30T10:00:00.000Z",
        [{"addr": "0x1234", "power": 1500, "voltage": 23000, "current": 650, "energy": 4200}],
    )


@pytest.mark.asyncio
async def test_a_plug_becomes_a_state_sensor_and_two_readings_sensors(hass, tmp_path):
    entry, zigbee, _packs, coordinator, added = await _setup(hass, tmp_path)
    await _add_plug(zigbee)
    await coordinator.async_refresh()

    plug = {uid: e for uid, e in added.items() if PLUG_ID in uid}
    assert sorted(uid.rsplit("_", 1)[1] for uid in plug) == ["energy", "power", "state"]

    state = plug[f"{entry.entry_id}_{PLUG_ID}_state"]
    assert state.is_on is True
    assert state.available is True

    power = plug[f"{entry.entry_id}_{PLUG_ID}_power"]
    assert power.native_value == 1500
    assert power.native_unit_of_measurement == "W"

    energy = plug[f"{entry.entry_id}_{PLUG_ID}_energy"]
    assert energy.native_value == 4200
    assert energy.state_class == "total_increasing"

    # Read-only: nothing a user could switch.
    from homeassistant.components.switch import SwitchEntity

    assert not any(isinstance(e, SwitchEntity) for e in added.values())

    # An unreachable plug shows as unavailable.
    await zigbee.async_update(PLUG_ID, {"reachable": False})
    await coordinator.async_refresh()
    assert state.available is False


@pytest.mark.asyncio
async def test_a_zigbee_sensor_becomes_temperature_humidity_and_battery(hass, tmp_path):
    entry, zigbee, _packs, coordinator, added = await _setup(hass, tmp_path)
    await zigbee.async_add(BOX, "00:12:4b:00:aa:bb:cc:dd", "0x5678", 1)
    await zigbee.async_update("zb-00124b00aabbccdd", {"caps": 0x98})
    await zigbee.async_store_sensor_readings(
        BOX, [{"addr": "0x5678", "ageMs": 0, "temperature": 2150, "humidity": 4800, "battery": 180}]
    )
    await coordinator.async_refresh()

    sensor = {uid.rsplit("_", 1)[1]: e for uid, e in added.items() if "zb-00124b00aabbccdd" in uid}
    assert sorted(sensor) == ["battery", "humidity", "temperature"]
    assert sensor["temperature"].native_value == 21.5
    assert sensor["humidity"].native_value == 48.0
    assert sensor["battery"].native_value == 90


@pytest.mark.asyncio
async def test_a_packs_cells_become_sensors(hass, tmp_path):
    entry, _zigbee, packs, coordinator, added = await _setup(hass, tmp_path)
    await packs.async_store_report(BOX, _bms_report())
    await coordinator.async_refresh()

    pack_id = "a4c138000001"
    pack = {uid: e for uid, e in added.items() if pack_id in uid}
    keys = {uid.split(f"{pack_id}_", 1)[1]: e for uid, e in pack.items()}
    assert {"soc", "voltage", "current"} <= set(keys)
    cells = sorted(k for k in keys if k.startswith("cell_"))
    assert len(cells) == 16
    assert keys["soc"].native_value == 81
    assert keys["voltage"].native_value == 53.12
    assert keys["current"].native_value == 12.5
    assert keys["cell_1"].native_value == 3.32
    assert keys["cell_4"].native_value == 3.322
    assert keys["cell_1"].device_info["identifiers"] == {(DOMAIN, f"bms-{pack_id}")}

    # A pack the box holds but never read: SoC, voltage and current only,
    # unavailable until it reports.
    unread = {
        uid.split("a4c138000002_", 1)[1]: e for uid, e in added.items() if "a4c138000002" in uid
    }
    assert sorted(unread) == ["current", "soc", "voltage"]
    assert unread["soc"].available is False


@pytest.mark.asyncio
async def test_entities_follow_the_roster(hass, tmp_path):
    entry, zigbee, packs, coordinator, added = await _setup(hass, tmp_path)
    assert added == {}

    await _add_plug(zigbee)
    await packs.async_store_report(BOX, _bms_report())
    await coordinator.async_refresh()
    tracked = hass.data[DOMAIN][entry.entry_id]["edge_entity_trackers"]
    assert any(PLUG_ID in uid for t in tracked for uid in t.entities)
    assert any("a4c138000002" in uid for t in tracked for uid in t.entities)

    # The plug is removed, and the owner forgets a pack.
    await zigbee.async_remove(PLUG_ID)
    await packs.async_change_roster(
        BOX, lambda ps: [p for p in ps if p["packId"] != "a4c138000002"]
    )
    await coordinator.async_refresh()
    current = {uid for t in tracked for uid in t.entities}
    assert not any(PLUG_ID in uid for uid in current)
    assert not any("a4c138000002" in uid for uid in current)
    assert any("a4c138000001" in uid for uid in current)

    # It joins again: its entities come back.
    await _add_plug(zigbee)
    await coordinator.async_refresh()
    assert any(PLUG_ID in uid for t in tracked for uid in t.entities)


@pytest.mark.asyncio
async def test_no_adopted_box_adds_no_edge_entities(hass):
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={"inverters": []})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "activity": ActivityTracker(),
        "entry_data": dict(entry.data),
        "edge_entities": None,
    }
    added = []
    await sensor_mod.async_setup_entry(hass, entry, lambda es, *a, **k: added.extend(es))
    await binary_sensor_mod.async_setup_entry(hass, entry, lambda es, *a, **k: added.extend(es))
    assert added == []
