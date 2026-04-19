"""Svitgrid custom component — B1 MVP."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN  # noqa: F401  (re-exported for Task 10)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Placeholder — filled in during Task 10."""
    _LOGGER.info("Svitgrid setup called (B1 MVP scaffold)")
    return True
