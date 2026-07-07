"""Problem binary_sensor reflecting device lifecycle (deprovision reaction)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .activity import ActivityTracker
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class SvitgridProblemBinarySensor(BinarySensorEntity):
    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_name = "Problem"
    _attr_translation_key = "problem"

    def __init__(
        self,
        activity: ActivityTracker,
        entry_id: str,
        inverter_id: str,
        label: str,
    ) -> None:
        self._activity = activity
        self._attr_unique_id = f"{entry_id}_{inverter_id}_problem"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, inverter_id)},
            name=label,
            manufacturer="Svitgrid",
            model="HA Add-on",
        )

    @property
    def is_on(self) -> bool:
        return self._activity.lifecycle_state != "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "lifecycle_state": self._activity.lifecycle_state,
            "reason": self._activity.lifecycle_reason,
        }

    async def async_update(self) -> None:
        return


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not state or "activity" not in state:
        _LOGGER.warning(
            "Svitgrid binary_sensor setup: no activity tracker for entry %s", entry.entry_id
        )
        return
    activity: ActivityTracker = state["activity"]
    from . import _inverters_from_entry  # local import avoids a circular import

    entities: list[BinarySensorEntity] = []
    for inv in _inverters_from_entry(entry):
        inverter_id = inv["inverter_id"]
        label = f"{inv.get('brand') or 'Svitgrid'} {inv.get('model') or ''}".strip()
        entities.append(SvitgridProblemBinarySensor(activity, entry.entry_id, inverter_id, label))
    async_add_entities(entities)
