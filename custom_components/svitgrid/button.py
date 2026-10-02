"""Read-now button: one immediate poll of a direct-harvest inverter."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .harvest.read_now import ReadNowTrigger


class SvitgridReadNowButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Read now"
    _attr_translation_key = "read_now"
    _attr_icon = "mdi:refresh"

    def __init__(
        self,
        trigger: ReadNowTrigger,
        entry_id: str,
        inverter_id: str,
        label: str,
    ) -> None:
        self._trigger = trigger
        self._attr_unique_id = f"{entry_id}_{inverter_id}_read_now"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, inverter_id)},
            name=label,
            manufacturer="Svitgrid",
            model="HA Add-on",
        )

    async def async_press(self) -> None:
        # Refused (None) while a poll is already in flight: that poll is the
        # read the owner asked for.
        self._trigger.request()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    triggers: dict[str, ReadNowTrigger] = state.get("read_now") or {}
    if not triggers:
        return
    from . import _inverters_from_entry  # local import avoids a circular import

    labels = {
        inv["inverter_id"]: f"{inv.get('brand') or 'Svitgrid'} {inv.get('model') or ''}".strip()
        for inv in _inverters_from_entry(entry)
    }
    async_add_entities(
        SvitgridReadNowButton(trigger, entry.entry_id, inv_id, labels.get(inv_id, "Svitgrid"))
        for inv_id, trigger in triggers.items()
    )
