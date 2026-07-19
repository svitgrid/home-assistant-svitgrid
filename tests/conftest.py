"""Pytest fixtures for Svitgrid tests."""

from __future__ import annotations

import sys
from typing import NamedTuple

# Mock StaticPathConfig for Home Assistant compatibility.
# HA 2024.3.3 doesn't export this; tests that need it will use a fake.
class StaticPathConfig(NamedTuple):
    url_path: str
    path: str
    cache: bool

# Patch before HA components load
import homeassistant.components.http
homeassistant.components.http.StaticPathConfig = StaticPathConfig

# pytest-homeassistant-custom-component auto-provides the `hass` fixture.
# No extra fixtures needed yet — tests add their own.

pytest_plugins = ["pytest_homeassistant_custom_component"]
