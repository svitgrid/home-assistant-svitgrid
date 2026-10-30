"""Home Assistant entities for the adopted edge boxes' Zigbee devices and
battery packs (edge island stage 2B, plan Task 49).

Per plug: a `binary_sensor` for its on/off state and `sensor`s for power and
energy. Per Zigbee sensor: temperature, humidity and battery. Per pack: SoC,
voltage, current and one sensor per cell voltage. Read from the stores the
edge views fill (`zigbee_devices.py`, `bms_packs.py`) every
`_UPDATE_INTERVAL_S`, and added or removed as the roster changes.

Read-only on purpose: the box refuses an unsigned `set_state`
(`command_auth.c:49`) and the integration holds no key the box trusts, so
there is no `switch`.

Only an entry with an adopted box has a coordinator; without one the
platforms add nothing here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_UPDATE_INTERVAL_S = 30

# `caps` bits (`zb_device_profile.h`, as the app's `pairable_device.dart`).
_CAP_ON_OFF = 1 << 0


def _is_sensor(item: dict) -> bool:
    """A device that has sent a sensor reading is a sensor (as `zigbee_lan`
    shapes it); so is one whose known capabilities carry no On/Off cluster,
    before its first reading. `caps == 0` means not discovered yet."""
    if item["sensor"] is not None:
        return True
    caps = item["device"].get("caps")
    return isinstance(caps, int) and caps != 0 and not caps & _CAP_ON_OFF


class EdgeEntitiesCoordinator(DataUpdateCoordinator[dict]):
    """A snapshot of every adopted box's Zigbee devices and battery packs:
    `{"zigbee": {zb_id: {device, socket, sensor}}, "packs": {pack_id:
    {boxId, label, pack}}}`."""

    def __init__(self, hass: HomeAssistant, zigbee_devices, bms_packs) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="svitgrid_edge_entities",
            update_interval=timedelta(seconds=_UPDATE_INTERVAL_S),
        )
        self._zigbee = zigbee_devices
        self._packs = bms_packs

    async def _async_update_data(self) -> dict:
        zigbee: dict[str, dict] = {}
        if self._zigbee is not None:
            for device in await self._zigbee.async_list():
                socket, sensor = await self._zigbee.async_latest(device["id"])
                zigbee[device["id"]] = {"device": device, "socket": socket, "sensor": sensor}
        packs: dict[str, dict] = {}
        if self._packs is not None:
            states = await self._packs.async_states()
            rosters = await self._packs.async_rosters()
            for box_id in sorted(set(states) | set(rosters)):
                reported = {p["id"]: p for p in (states.get(box_id) or {}).get("packs", [])}
                # The roster Home Assistant keeps for the box, else the packs
                # the box last reported holding.
                if box_id in rosters:
                    roster = [(p["packId"], p.get("label")) for p in rosters[box_id]["packs"]]
                else:
                    roster = [(pid, p.get("label")) for pid, p in reported.items()]
                for pack_id, label in roster:
                    packs[pack_id] = {
                        "boxId": box_id,
                        "label": label,
                        "pack": reported.get(pack_id),
                    }
        return {"zigbee": zigbee, "packs": packs}


# ── entities ──────────────────────────────────────────────────────────────────


class _EdgeEntity(CoordinatorEntity[EdgeEntitiesCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id: str, object_id: str, key: str, name: str):
        super().__init__(coordinator)
        self._object_id = object_id
        self._key = key
        self._attr_unique_id = f"{entry_id}_{object_id}_{key}"
        self._attr_name = name


class _ZigbeeEntity(_EdgeEntity):
    def __init__(self, coordinator, entry_id: str, zb_id: str, key: str, name: str):
        super().__init__(coordinator, entry_id, zb_id, key, name)
        device = coordinator.data["zigbee"][zb_id]["device"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, zb_id)},
            name=device.get("name") or device.get("model") or zb_id,
            manufacturer=device.get("manufacturer") or "Zigbee",
            model=device.get("model"),
        )

    @property
    def _item(self) -> dict | None:
        return ((self.coordinator.data or {}).get("zigbee") or {}).get(self._object_id)

    @property
    def available(self) -> bool:
        item = self._item
        return (
            super().available and item is not None and item["device"].get("reachable") is not False
        )


class ZigbeePlugStateBinarySensor(_ZigbeeEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.POWER

    @property
    def is_on(self) -> bool | None:
        item = self._item
        return item["device"].get("on") if item else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = (self._item or {}).get("device") or {}
        return {
            "reachable": device.get("reachable"),
            "unreachable_since": device.get("unreachableSince"),
        }


class ZigbeeSensor(_ZigbeeEntity, SensorEntity):
    """One reading of a plug (`socket`) or a sensor (`sensor`), scaled."""

    def __init__(self, coordinator, entry_id, zb_id, key, name, source, field, scale, **attrs):
        super().__init__(coordinator, entry_id, zb_id, key, name)
        self._source = source
        self._field = field
        self._scale = scale
        for attr, value in attrs.items():
            setattr(self, f"_attr_{attr}", value)

    @property
    def native_value(self):
        reading = (self._item or {}).get(self._source)
        value = reading.get(self._field) if reading else None
        return self._scale(value) if value is not None else None


class PackSensor(_EdgeEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry_id, pack_id, key, name, value_of, **attrs):
        super().__init__(coordinator, entry_id, f"bms-{pack_id}", key, name)
        self._pack_id = pack_id
        self._value_of = value_of
        for attr, value in attrs.items():
            setattr(self, f"_attr_{attr}", value)
        item = coordinator.data["packs"][pack_id]
        self._attr_unique_id = f"{entry_id}_{pack_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"bms-{pack_id}")},
            name=item.get("label") or (item.get("pack") or {}).get("name") or pack_id,
            manufacturer="Svitgrid",
            model="Battery pack",
        )

    @property
    def _pack(self) -> dict | None:
        item = ((self.coordinator.data or {}).get("packs") or {}).get(self._pack_id)
        return (item or {}).get("pack")

    @property
    def native_value(self):
        pack = self._pack
        return self._value_of(pack) if pack else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


def _milli(value) -> float | None:
    return value / 1000 if isinstance(value, int | float) and not isinstance(value, bool) else None


def _cell(index: int) -> Callable[[dict], float | None]:
    def value_of(pack: dict) -> float | None:
        cells = pack.get("cellsMv")
        return _milli(cells[index]) if isinstance(cells, list) and index < len(cells) else None

    return value_of


_VOLTS = {
    "device_class": SensorDeviceClass.VOLTAGE,
    "native_unit_of_measurement": UnitOfElectricPotential.VOLT,
}


def _sensor_factories(coordinator, entry_id: str) -> dict[str, Callable[[], Any]]:
    """Every `sensor` the snapshot calls for, by unique id."""
    out: dict[str, Callable[[], Any]] = {}
    data = coordinator.data or {}

    def add(zb_id, key, *args, **attrs):
        out[f"{entry_id}_{zb_id}_{key}"] = lambda: ZigbeeSensor(
            coordinator, entry_id, zb_id, key, *args, **attrs
        )

    for zb_id, item in (data.get("zigbee") or {}).items():
        if _is_sensor(item):
            add(
                zb_id, "temperature", "Temperature",
                "sensor", "temperature", lambda v: v / 100,
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                state_class=SensorStateClass.MEASUREMENT,
            )  # fmt: skip
            add(
                zb_id, "humidity", "Humidity",
                "sensor", "humidity", lambda v: v / 100,
                device_class=SensorDeviceClass.HUMIDITY,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            )  # fmt: skip
            # ZCL battery is in half percent, as the app reads it.
            add(
                zb_id, "battery", "Battery",
                "sensor", "battery", lambda v: round(v / 2),
                device_class=SensorDeviceClass.BATTERY,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            )  # fmt: skip
        else:
            add(
                zb_id, "power", "Power",
                "socket", "power", lambda v: v,
                device_class=SensorDeviceClass.POWER,
                native_unit_of_measurement=UnitOfPower.WATT,
                state_class=SensorStateClass.MEASUREMENT,
            )  # fmt: skip
            # Scaled by the plug's profile into Wh, as the usage rollup's
            # `energyCounterWh`.
            add(
                zb_id, "energy", "Energy",
                "socket", "energy", lambda v: v,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
                state_class=SensorStateClass.TOTAL_INCREASING,
            )  # fmt: skip

    for pack_id, item in (data.get("packs") or {}).items():
        out[f"{entry_id}_{pack_id}_soc"] = lambda pid=pack_id: PackSensor(
            coordinator, entry_id, pid, "soc", "State of charge",
            lambda p: p.get("soc"),
            device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
        )  # fmt: skip
        out[f"{entry_id}_{pack_id}_voltage"] = lambda pid=pack_id: PackSensor(
            coordinator, entry_id, pid, "voltage", "Voltage",
            lambda p: _milli(p.get("packMv")), **_VOLTS,
        )  # fmt: skip
        out[f"{entry_id}_{pack_id}_current"] = lambda pid=pack_id: PackSensor(
            coordinator, entry_id, pid, "current", "Current",
            lambda p: _milli(p.get("currentMa")),
            device_class=SensorDeviceClass.CURRENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        )  # fmt: skip
        cells = (item.get("pack") or {}).get("cellsMv")
        for i in range(len(cells) if isinstance(cells, list) else 0):
            out[f"{entry_id}_{pack_id}_cell_{i + 1}"] = lambda pid=pack_id, i=i: PackSensor(
                coordinator, entry_id, pid, f"cell_{i + 1}", f"Cell {i + 1} voltage",
                _cell(i), **_VOLTS,
            )  # fmt: skip
    return out


def _binary_sensor_factories(coordinator, entry_id: str) -> dict[str, Callable[[], Any]]:
    """Every `binary_sensor` the snapshot calls for, by unique id."""
    return {
        f"{entry_id}_{zb_id}_state": (
            lambda zid=zb_id: ZigbeePlugStateBinarySensor(
                coordinator, entry_id, zid, "state", "State"
            )
        )
        for zb_id, item in ((coordinator.data or {}).get("zigbee") or {}).items()
        if not _is_sensor(item)
    }


_FACTORIES = {"sensor": _sensor_factories, "binary_sensor": _binary_sensor_factories}


class EdgeEntityTracker:
    """Adds the platform's entities the roster gains and removes the ones it
    loses, on every coordinator update."""

    def __init__(self, hass, entry_id: str, coordinator, platform: str, async_add_entities):
        self._hass = hass
        self._entry_id = entry_id
        self._coordinator = coordinator
        self._factories = _FACTORIES[platform]
        self._add = async_add_entities
        self.entities: dict[str, Any] = {}

    @callback
    def sync(self) -> None:
        wanted = self._factories(self._coordinator, self._entry_id)
        for uid in [u for u in self.entities if u not in wanted]:
            self._remove(self.entities.pop(uid))
        new = {uid: make() for uid, make in wanted.items() if uid not in self.entities}
        if new:
            self.entities.update(new)
            self._add(list(new.values()))

    @callback
    def _remove(self, entity) -> None:
        if entity.hass is None or entity.entity_id is None:
            return
        registry = er.async_get(self._hass)
        if registry.async_get(entity.entity_id) is not None:
            # Removing the registry entry removes the entity from the state
            # machine too.
            registry.async_remove(entity.entity_id)
        else:
            self._hass.async_create_task(entity.async_remove())


def async_setup_platform_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: str,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Track the edge entities of `platform` for an entry with an adopted box;
    nothing for any other entry."""
    state = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = state.get("edge_entities")
    if coordinator is None:
        return
    tracker = EdgeEntityTracker(hass, entry.entry_id, coordinator, platform, async_add_entities)
    state.setdefault("edge_entity_trackers", []).append(tracker)
    tracker.sync()
    entry.async_on_unload(coordinator.async_add_listener(tracker.sync))
