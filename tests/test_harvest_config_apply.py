"""Direct unit tests for `apply_harvest_config_change` (persistence bug fix).

Regression coverage for the "Important" review finding: the old
implementation did `list(entry.data.get("inverters", []))` — a SHALLOW copy
— then mutated the inner `harvest_config` dict IN PLACE before building
`new_data`. Because the inner dict objects are shared with `entry.data`,
`entry.data` already reflected the new values by the time
`async_update_entry(entry, data=new_data)` ran, so `new_data == entry.data`
and Home Assistant's `async_update_entry` treats that as a no-op — the
change was never actually persisted to `.storage/core.config_entries`.
Works in memory, silently reverts to the OLD connection on next HA restart.

Import note: we load `harvest_config_apply.py` directly by file path via
`importlib`, NOT via `from custom_components.svitgrid.harvest_config_apply
import ...`. That form of import is currently impossible to collect on this
repo's pinned test environment: `custom_components/svitgrid/__init__.py`
unconditionally imports `.panel`, which imports
`homeassistant.components.http.StaticPathConfig` — a symbol absent from the
`homeassistant==2024.3.3` pin that `pytest-homeassistant-custom-component`
resolves to on Python 3.11 (see `tests/test_command_poller_harvest_config.py`
and `.superpowers/sdd/task-4-report.md` in the sibling svitgrid repo for the
full pre-existing-issue writeup). Verified directly: `python -c "from
custom_components.svitgrid.harvest_config_apply import ..."` raises that
same `ImportError` with NO pytest involved at all — it is not a
collection-specific problem. `harvest_config_apply.py` itself has zero
relative/package imports (just `asyncio`, `contextlib`, `logging`), so
loading it by file path bypasses the package `__init__.py` entirely and
avoids needing any conftest shim, while still exercising the real,
unmodified module code.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "svitgrid"
    / "harvest_config_apply.py"
)
_spec = importlib.util.spec_from_file_location("harvest_config_apply_under_test", _MODULE_PATH)
_harvest_config_apply = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harvest_config_apply)

apply_harvest_config_change = _harvest_config_apply.apply_harvest_config_change
probe_modbus_reachable = _harvest_config_apply.probe_modbus_reachable


def _make_entry():
    """Fake ConfigEntry whose .data is a real nested dict (not a MagicMock)."""
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"inverters": [{"harvest_config": {"ip": "old", "port": 502, "slave_id": 1}}]}
    return entry


@pytest.mark.asyncio
async def test_apply_harvest_config_change_persists_new_connection():
    hass = MagicMock()
    entry = _make_entry()

    new_conn = {"ip": "192.168.1.50", "port": 1502, "slaveId": 7}
    await apply_harvest_config_change(hass, entry, new_conn)

    hass.config_entries.async_update_entry.assert_called_once()
    _, kwargs = hass.config_entries.async_update_entry.call_args
    persisted = kwargs["data"]
    persisted_hc = persisted["inverters"][0]["harvest_config"]
    assert persisted_hc["ip"] == "192.168.1.50"
    assert persisted_hc["port"] == 1502
    assert persisted_hc["slave_id"] == 7


@pytest.mark.asyncio
async def test_apply_harvest_config_change_does_not_mutate_caller_dict():
    """Regression assertion: fails on the pre-fix code.

    The old code mutated the SAME inner dict object referenced by
    entry.data before async_update_entry ever ran, so by the time the new
    `data=` kwarg was built it was already identical to entry.data (no-op
    persist). Capture a reference to the original inner harvest_config
    dict BEFORE calling, and assert it still holds the OLD values
    afterward — proving the function did not mutate the caller's object
    graph in place.
    """
    hass = MagicMock()
    entry = _make_entry()
    original_hc = entry.data["inverters"][0]["harvest_config"]
    original_hc_snapshot = copy.deepcopy(original_hc)

    new_conn = {"ip": "192.168.1.50", "port": 1502, "slaveId": 7}
    await apply_harvest_config_change(hass, entry, new_conn)

    # The SAME dict object we grabbed before the call must be untouched.
    assert original_hc == original_hc_snapshot
    assert original_hc["ip"] == "old"
    assert original_hc["port"] == 502
    assert original_hc["slave_id"] == 1


@pytest.mark.asyncio
async def test_apply_harvest_config_change_schedules_reload():
    hass = MagicMock()
    entry = _make_entry()

    new_conn = {"ip": "192.168.1.50", "port": 1502, "slaveId": 7}
    await apply_harvest_config_change(hass, entry, new_conn)

    hass.async_create_task.assert_called_once()
    hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_probe_modbus_reachable_returns_false_on_connect_failure(
    monkeypatch,
):
    async def _raise(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(_harvest_config_apply.asyncio, "open_connection", _raise)

    result = await probe_modbus_reachable("10.0.0.9", 502)
    assert result is False
