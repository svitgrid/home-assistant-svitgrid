"""Svitgrid status sensors — surfaced on the integration's device page.

Six entities so the user can see at a glance whether the integration
is healthy without leaving HA:
- sensor.svitgrid_status         — "ok" | "error" | "idle"
- sensor.svitgrid_last_ingest_at — timestamp of most recent ingest
- sensor.svitgrid_ingests_24h    — rolling count, last 24h
- sensor.svitgrid_last_command_at — timestamp of most recent command
- sensor.svitgrid_commands_24h   — rolling count, last 24h
- sensor.svitgrid_diagnostics    — status line + recent ingest log (incl. skips)

The last three sensors also carry attribute dicts (`recent`) with the
last 10 events each — that's where the user sees the rolling history.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .activity import ActivityTracker
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# How often the sensor entities re-poll their backing ActivityTracker.
# Tracker mutations are sync (in publisher/poller callbacks); we don't
# get push-style updates, so a poll keeps the UI fresh.
_UPDATE_INTERVAL_S = 30


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not state or "activity" not in state:
        _LOGGER.warning(
            "Svitgrid sensor platform setup: no activity tracker for entry %s",
            entry.entry_id,
        )
        return
    activity: ActivityTracker = state["activity"]
    from . import _inverters_from_entry  # local import avoids a circular import

    entities: list[SensorEntity] = []
    for inv in _inverters_from_entry(entry):
        inverter_id = inv["inverter_id"]
        label = f'{inv.get("brand") or "Svitgrid"} {inv.get("model") or ""}'.strip()
        entities.extend([
            StatusSensor(activity, entry.entry_id, inverter_id, label),
            LastIngestAtSensor(activity, entry.entry_id, inverter_id, label),
            Ingests24hSensor(activity, entry.entry_id, inverter_id, label),
            LastCommandAtSensor(activity, entry.entry_id, inverter_id, label),
            Commands24hSensor(activity, entry.entry_id, inverter_id, label),
            DiagnosticsSensor(activity, entry.entry_id, inverter_id, label),
        ])
    async_add_entities(entities)


class _SvitgridSensorBase(SensorEntity):
    """Shared device-info + polling cadence. One HA device per inverter so all
    five sensors for a given inverter group under one card on the device page."""

    _attr_should_poll = True
    _attr_has_entity_name = True

    def __init__(
        self,
        activity: ActivityTracker,
        entry_id: str,
        inverter_id: str,
        label: str,
    ) -> None:
        self._activity = activity
        self._entry_id = entry_id
        self._inverter_id = inverter_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, inverter_id)},
            name=label,
            manufacturer="Svitgrid",
            model="HA Add-on",
        )

    async def async_update(self) -> None:
        # ActivityTracker is in-memory; just trigger HA's poll-tick.
        # The state/extra_state_attributes properties re-read from the
        # tracker each time. No-op body — the framework picks up changes.
        return


class StatusSensor(_SvitgridSensorBase):
    _attr_translation_key = "status"
    _attr_icon = "mdi:cloud-check"

    def __init__(self, activity, entry_id, inverter_id, label):
        super().__init__(activity, entry_id, inverter_id, label)
        self._attr_unique_id = f"{entry_id}_{inverter_id}_status"
        self._attr_name = "Status"

    @property
    def native_value(self) -> str:
        return self._activity.status


class LastIngestAtSensor(_SvitgridSensorBase):
    _attr_translation_key = "last_ingest_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:cloud-upload"

    def __init__(self, activity, entry_id, inverter_id, label):
        super().__init__(activity, entry_id, inverter_id, label)
        self._attr_unique_id = f"{entry_id}_{inverter_id}_last_ingest_at"
        self._attr_name = "Last ingest"

    @property
    def native_value(self) -> datetime | None:
        return self._activity.last_ingest_at


class Ingests24hSensor(_SvitgridSensorBase):
    _attr_translation_key = "ingests_24h"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "ingests"

    def __init__(self, activity, entry_id, inverter_id, label):
        super().__init__(activity, entry_id, inverter_id, label)
        self._attr_unique_id = f"{entry_id}_{inverter_id}_ingests_24h"
        self._attr_name = "Ingests (24h)"

    @property
    def native_value(self) -> int:
        return self._activity.ingest_count_24h

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Rolling buffer of last 10 ingests — visible on the entity's
        # attributes panel. Lets the user inspect what landed (and what
        # didn't) without opening logs.
        return {"recent": list(self._activity.recent_ingests())}


class LastCommandAtSensor(_SvitgridSensorBase):
    _attr_translation_key = "last_command_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:console-line"

    def __init__(self, activity, entry_id, inverter_id, label):
        super().__init__(activity, entry_id, inverter_id, label)
        self._attr_unique_id = f"{entry_id}_{inverter_id}_last_command_at"
        self._attr_name = "Last command"

    @property
    def native_value(self) -> datetime | None:
        return self._activity.last_command_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"last_kind": self._activity.last_command_kind}


class Commands24hSensor(_SvitgridSensorBase):
    _attr_translation_key = "commands_24h"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = "commands"

    def __init__(self, activity, entry_id, inverter_id, label):
        super().__init__(activity, entry_id, inverter_id, label)
        self._attr_unique_id = f"{entry_id}_{inverter_id}_commands_24h"
        self._attr_name = "Commands (24h)"

    @property
    def native_value(self) -> int:
        return self._activity.command_count_24h

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"recent": list(self._activity.recent_commands())}


class DiagnosticsSensor(_SvitgridSensorBase):
    """Human-readable status line + recent ingest log for copy-paste support.
    State shows e.g. "waiting — incomplete reading; missing: batterySoc";
    the `recent` attribute carries the last 10 ingest outcomes (incl. skips
    with their missing fields and culprit entities)."""

    _attr_translation_key = "diagnostics"
    _attr_icon = "mdi:text-box-search"

    def __init__(self, activity, entry_id, inverter_id, label):
        super().__init__(activity, entry_id, inverter_id, label)
        self._attr_unique_id = f"{entry_id}_{inverter_id}_diagnostics"
        self._attr_name = "Diagnostics"

    @property
    def native_value(self) -> str:
        return self._activity.diagnostics_line()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "recent": list(self._activity.recent_ingests()),
            "ingests_24h": self._activity.ingest_count_24h,
        }
