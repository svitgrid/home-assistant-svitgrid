# Edit Sensor Mappings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Home Assistant Options Flow so a paired user can edit which HA sensor feeds each Svitgrid field, after pairing, with the running publisher picking up the change automatically.

**Architecture:** A single ordered `MAPPABLE_FIELDS` source of truth in `const.py` drives both the existing manual pairing form and a new options ("Configure") form. The options flow writes the edited map to `entry.options["entity_map"]`; `async_setup_entry` reads options-first; an entry-reload update listener restarts the publisher when options change. Local only — no API/server changes.

**Tech Stack:** Python, Home Assistant `config_entries` (ConfigFlow + OptionsFlow), `voluptuous`, `homeassistant.helpers.selector.EntitySelector`, pytest + `pytest_homeassistant_custom_component`.

**Working directory:** `/Users/ivanursul/git/home-assistant-svitgrid/.worktrees/edit-sensor-mappings` (branch `feat/edit-sensor-mappings`). Run tests with the worktree's interpreter: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-05-29-edit-sensor-mappings-design.md`

---

## File Structure

- `custom_components/svitgrid/const.py` — add `MAPPABLE_FIELDS` (ordered `(field, label)` list, single source of truth).
- `custom_components/svitgrid/config_flow.py` — derive `_MANUAL_FIELDS` from `MAPPABLE_FIELDS`; add `async_get_options_flow` + `SvitgridOptionsFlow`.
- `custom_components/svitgrid/__init__.py` — read `entity_map` options-first; register entry-reload update listener.
- `custom_components/svitgrid/strings.json` + `custom_components/svitgrid/translations/en.json` — add the 7 missing `manual_entities` labels; add the `options` section.
- `tests/test_const.py` — NEW: guard that `MAPPABLE_FIELDS` covers `ALL_FIELDS`.
- `tests/test_config_flow.py` — derive-from-source guard + options flow tests.
- `tests/test_init.py` — options-first precedence + reload-on-options-change.

---

## Task 1: Single source of truth — `MAPPABLE_FIELDS`

**Files:**
- Modify: `custom_components/svitgrid/const.py` (after the `ALL_FIELDS` block, ~line 45)
- Test: `tests/test_const.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_const.py`:

```python
"""Guards for the canonical-field constants."""
from __future__ import annotations

from custom_components.svitgrid.const import ALL_FIELDS, MAPPABLE_FIELDS


def test_mappable_fields_cover_all_fields_exactly():
    """MAPPABLE_FIELDS is the single source of truth — it must cover every
    canonical field, with no extras and no duplicates."""
    keys = [field for field, _label in MAPPABLE_FIELDS]
    assert set(keys) == ALL_FIELDS
    assert len(keys) == len(set(keys)), "duplicate field in MAPPABLE_FIELDS"


def test_mappable_fields_have_nonempty_labels():
    for field, label in MAPPABLE_FIELDS:
        assert isinstance(label, str) and label.strip(), f"empty label for {field}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_const.py -v`
Expected: FAIL with `ImportError: cannot import name 'MAPPABLE_FIELDS'`.

- [ ] **Step 3: Add `MAPPABLE_FIELDS` to `const.py`**

Insert immediately after the `ALL_FIELDS = ...` block (before the `READING_SOURCE` comment):

```python
# Ordered (field, human label) list — the single source of truth for which
# canonical fields can be mapped to a Home Assistant sensor and what we call
# them in the UI. Both the manual pairing step (config flow) and the options
# (edit) flow derive their forms from this list, so the two can never drift.
# Grouped: battery → PV strings → grid → load → daily energy → temps/frequency.
# The key set MUST equal ALL_FIELDS (locked by tests/test_const.py).
MAPPABLE_FIELDS: list[tuple[str, str]] = [
    ("batterySoc", "Battery state of charge (%)"),
    ("batteryPower", "Battery power (W — positive = charging)"),
    ("batteryVoltage", "Battery voltage (V)"),
    ("batteryCurrent", "Battery current (A — positive = charging)"),
    ("batteryTemperature", "Battery temperature (°C)"),
    ("pv1Power", "PV string 1 power (W)"),
    ("pv2Power", "PV string 2 power (W)"),
    ("pv3Power", "PV string 3 power (W)"),
    ("pv4Power", "PV string 4 power (W)"),
    ("gridPower", "Grid power (W — positive = import)"),
    ("gridVoltageL1", "Grid voltage L1 (V)"),
    ("gridVoltageL2", "Grid voltage L2 (V)"),
    ("gridVoltageL3", "Grid voltage L3 (V)"),
    ("gridFrequency", "Grid frequency (Hz)"),
    ("loadPower", "Load power (W)"),
    ("dailyPvEnergy", "Daily PV production (kWh)"),
    ("dailyGridImportEnergy", "Daily grid import (kWh)"),
    ("dailyGridExportEnergy", "Daily grid export (kWh)"),
    ("dailyLoadEnergy", "Daily load energy (kWh)"),
    ("inverterTemperature", "Inverter temperature (°C)"),
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_const.py -v`
Expected: PASS (2 tests). If `test_mappable_fields_cover_all_fields_exactly` fails on a set mismatch, the `MAPPABLE_FIELDS` keys drifted from `ALL_FIELDS` — reconcile the two lists.

- [ ] **Step 5: Commit**

```bash
cd /Users/ivanursul/git/home-assistant-svitgrid/.worktrees/edit-sensor-mappings
git add custom_components/svitgrid/const.py tests/test_const.py
git commit -m "feat(const): add MAPPABLE_FIELDS single source of truth for mappable fields"
```

---

## Task 2: Derive the manual pairing form from `MAPPABLE_FIELDS`

Removes the duplicated 13-field `_MANUAL_FIELDS` list and the strings drift (manual flow now offers all 20 fields).

**Files:**
- Modify: `custom_components/svitgrid/config_flow.py:53-70` (the `_MANUAL_FIELDS` block) + the `const` import at line 36
- Modify: `custom_components/svitgrid/strings.json` (`config.step.manual_entities.data`)
- Modify: `custom_components/svitgrid/translations/en.json` (same section)
- Test: `tests/test_config_flow.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_flow.py`:

```python
def test_manual_fields_derive_from_mappable_source():
    """The manual pairing field list must be exactly MAPPABLE_FIELDS — no
    separate hardcoded copy that can drift."""
    from custom_components.svitgrid.config_flow import _MANUAL_FIELDS
    from custom_components.svitgrid.const import MAPPABLE_FIELDS

    assert list(_MANUAL_FIELDS) == list(MAPPABLE_FIELDS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_config_flow.py::test_manual_fields_derive_from_mappable_source -v`
Expected: FAIL (`_MANUAL_FIELDS` is the old 13-tuple, not equal to the 20-entry `MAPPABLE_FIELDS`).

- [ ] **Step 3: Refactor `_MANUAL_FIELDS` to derive from the source**

In `config_flow.py`, add `MAPPABLE_FIELDS` to the existing `from .const import (...)` block (line 36):

```python
from .const import (
    DEFAULT_API_BASE,
    DOMAIN,
    MAPPABLE_FIELDS,
    PAIRING_MAX_POLL_DURATION_S,
    PAIRING_POLL_INTERVAL_S,
)
```

Replace the entire `_MANUAL_FIELDS = ( ... )` block (lines 53-70) with:

```python
# Every Svitgrid reading field the publisher knows about, sourced from the
# single canonical list in const.py so the manual pairing form and the options
# (edit) form share one definition. Manual mode offers an EntitySelector per
# field; the user may leave any blank. At least one must be set (validated at
# form submit in async_step_manual_entities).
_MANUAL_FIELDS = tuple(MAPPABLE_FIELDS)
```

- [ ] **Step 4: Add the 7 new field labels to both strings files**

In `custom_components/svitgrid/strings.json`, replace the `config.step.manual_entities.data` object with all 20 labels (existing 13 + the 7 below), keeping it in `MAPPABLE_FIELDS` order:

```json
        "data": {
          "batterySoc": "Battery state of charge (%)",
          "batteryPower": "Battery power (W — positive = charging)",
          "batteryVoltage": "Battery voltage (V)",
          "batteryCurrent": "Battery current (A — positive = charging)",
          "batteryTemperature": "Battery temperature (°C)",
          "pv1Power": "PV string 1 power (W)",
          "pv2Power": "PV string 2 power (W)",
          "pv3Power": "PV string 3 power (W)",
          "pv4Power": "PV string 4 power (W)",
          "gridPower": "Grid power (W — positive = import)",
          "gridVoltageL1": "Grid voltage L1 (V)",
          "gridVoltageL2": "Grid voltage L2 (V)",
          "gridVoltageL3": "Grid voltage L3 (V)",
          "gridFrequency": "Grid frequency (Hz)",
          "loadPower": "Load power (W)",
          "dailyPvEnergy": "Daily PV production (kWh)",
          "dailyGridImportEnergy": "Daily grid import (kWh)",
          "dailyGridExportEnergy": "Daily grid export (kWh)",
          "dailyLoadEnergy": "Daily load energy (kWh)",
          "inverterTemperature": "Inverter temperature (°C)"
        }
```

Apply the **identical** replacement to `custom_components/svitgrid/translations/en.json` (same `config.step.manual_entities.data` path).

- [ ] **Step 5: Run tests to verify they pass**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_config_flow.py -v`
Expected: PASS — the new derive test passes and all pre-existing config-flow tests still pass.

Validate both JSON files parse:
Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/python -c "import json; json.load(open('custom_components/svitgrid/strings.json')); json.load(open('custom_components/svitgrid/translations/en.json')); print('json ok')"`
Expected: `json ok`

- [ ] **Step 6: Commit**

```bash
cd /Users/ivanursul/git/home-assistant-svitgrid/.worktrees/edit-sensor-mappings
git add custom_components/svitgrid/config_flow.py custom_components/svitgrid/strings.json custom_components/svitgrid/translations/en.json tests/test_config_flow.py
git commit -m "refactor(config_flow): derive manual field list from const.MAPPABLE_FIELDS"
```

---

## Task 3: Options flow — render the pre-filled edit form

**Files:**
- Modify: `custom_components/svitgrid/config_flow.py` (add `callback` import; add `async_get_options_flow` staticmethod to `SvitgridConfigFlow`; add `SvitgridOptionsFlow` class at end of file)
- Modify: `custom_components/svitgrid/strings.json` (add top-level `options` section)
- Modify: `custom_components/svitgrid/translations/en.json` (same)
- Test: `tests/test_config_flow.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_flow.py`:

```python
def test_current_map_prefers_options_over_data():
    """The edit form pre-fills from entry.options when present, else entry.data."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid.config_flow import SvitgridOptionsFlow

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entity_map": {"batterySoc": "sensor.from_data"}},
        options={"entity_map": {"batterySoc": "sensor.from_options"}},
    )
    flow = SvitgridOptionsFlow(entry)
    assert flow._current_map() == {"batterySoc": "sensor.from_options"}


def test_current_map_falls_back_to_data():
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid.config_flow import SvitgridOptionsFlow

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entity_map": {"gridPower": "sensor.grid"}},
        options={},
    )
    flow = SvitgridOptionsFlow(entry)
    assert flow._current_map() == {"gridPower": "sensor.grid"}


@pytest.mark.asyncio
async def test_options_flow_shows_init_form(hass: HomeAssistant, enable_custom_integrations) -> None:
    """Clicking Configure renders the init form."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entity_map": {"batterySoc": "sensor.soc"}},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_config_flow.py::test_current_map_prefers_options_over_data tests/test_config_flow.py::test_options_flow_shows_init_form -v`
Expected: FAIL (`cannot import name 'SvitgridOptionsFlow'`; options flow not registered).

- [ ] **Step 3: Add the `callback` import**

In `config_flow.py`, add to the imports near the top (after the `from homeassistant.data_entry_flow import FlowResult` line):

```python
from homeassistant.core import callback
```

- [ ] **Step 4: Register the options flow on the config flow**

Inside `class SvitgridConfigFlow`, immediately after `VERSION = 1` (line 76), add:

```python
    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SvitgridOptionsFlow":
        """Expose the 'Configure' button so users can edit sensor mappings."""
        return SvitgridOptionsFlow(config_entry)
```

- [ ] **Step 5: Add the `SvitgridOptionsFlow` class**

Append at the END of `config_flow.py`:

```python
class SvitgridOptionsFlow(config_entries.OptionsFlow):
    """Edit the entity_map (Svitgrid field → HA sensor) after pairing.

    Local only: the edited map is written to entry.options["entity_map"]; the
    update listener in __init__.py reloads the entry so the readings publisher
    restarts with the new mapping. The server is never told (it only ever
    seeded the map at pairing).
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # Store privately rather than assigning self.config_entry (which newer
        # HA provides automatically and warns about reassigning).
        self._entry = config_entry

    def _current_map(self) -> dict[str, str]:
        """Current mapping, options taking precedence over the pairing-time data."""
        return dict(
            self._entry.options.get("entity_map")
            or self._entry.data.get("entity_map")
            or {}
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Single step: per-field EntitySelector, pre-filled with the current map.

        Clearing a field unmaps it. At least one entity must remain set."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = {field: eid for field, eid in user_input.items() if eid}
            if not cleaned:
                errors["base"] = "no_entities_selected"
            else:
                return self.async_create_entry(
                    title="", data={"entity_map": cleaned}
                )

        # Build one optional EntitySelector per mappable field. Pre-fill via
        # add_suggested_values_to_schema (NOT vol.Optional defaults) so that a
        # cleared field stays cleared instead of snapping back to its old value.
        schema = vol.Schema({
            vol.Optional(field): EntitySelector(
                EntitySelectorConfig(domain="sensor"),
            )
            for field, _label in _MANUAL_FIELDS
        })
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self._current_map()
            ),
            errors=errors,
        )
```

- [ ] **Step 6: Add the `options` section to both strings files**

In `custom_components/svitgrid/strings.json`, add a top-level `"options"` key as a sibling of `"config"` (insert after the closing brace of the `config` object):

```json
  "options": {
    "step": {
      "init": {
        "title": "Edit sensor mappings",
        "description": "Change which Home Assistant sensor feeds each Svitgrid field. Clear a field to stop sending it. At least one must be set. Saving reloads the integration so changes take effect immediately.",
        "data": {
          "batterySoc": "Battery state of charge (%)",
          "batteryPower": "Battery power (W — positive = charging)",
          "batteryVoltage": "Battery voltage (V)",
          "batteryCurrent": "Battery current (A — positive = charging)",
          "batteryTemperature": "Battery temperature (°C)",
          "pv1Power": "PV string 1 power (W)",
          "pv2Power": "PV string 2 power (W)",
          "pv3Power": "PV string 3 power (W)",
          "pv4Power": "PV string 4 power (W)",
          "gridPower": "Grid power (W — positive = import)",
          "gridVoltageL1": "Grid voltage L1 (V)",
          "gridVoltageL2": "Grid voltage L2 (V)",
          "gridVoltageL3": "Grid voltage L3 (V)",
          "gridFrequency": "Grid frequency (Hz)",
          "loadPower": "Load power (W)",
          "dailyPvEnergy": "Daily PV production (kWh)",
          "dailyGridImportEnergy": "Daily grid import (kWh)",
          "dailyGridExportEnergy": "Daily grid export (kWh)",
          "dailyLoadEnergy": "Daily load energy (kWh)",
          "inverterTemperature": "Inverter temperature (°C)"
        }
      }
    },
    "error": {
      "no_entities_selected": "Pick at least one sensor — otherwise the dashboard has nothing to show."
    }
  }
```

Apply the **identical** `options` section to `custom_components/svitgrid/translations/en.json`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_config_flow.py -v`
Expected: PASS (new options tests + all pre-existing).

Validate JSON parses:
Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/python -c "import json; json.load(open('custom_components/svitgrid/strings.json')); json.load(open('custom_components/svitgrid/translations/en.json')); print('json ok')"`
Expected: `json ok`

- [ ] **Step 8: Commit**

```bash
cd /Users/ivanursul/git/home-assistant-svitgrid/.worktrees/edit-sensor-mappings
git add custom_components/svitgrid/config_flow.py custom_components/svitgrid/strings.json custom_components/svitgrid/translations/en.json tests/test_config_flow.py
git commit -m "feat(config_flow): options flow renders pre-filled sensor-mapping form"
```

---

## Task 4: Options flow — submit validation & persistence

Verifies the submit branch added in Task 3: blanks dropped, ≥1 required, written to `entry.options`.

**Files:**
- Test: `tests/test_config_flow.py` (no production change — submit logic was written in Task 3 Step 5; this task locks its behavior)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_flow.py`:

```python
@pytest.mark.asyncio
async def test_options_flow_saves_and_drops_blanks(hass: HomeAssistant, enable_custom_integrations) -> None:
    """Submitting writes the cleaned map (blank selectors dropped) to options."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entity_map": {"batterySoc": "sensor.old_soc"}},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "batterySoc": "sensor.new_soc",
            "gridPower": "sensor.grid",
            "loadPower": "",  # blank → dropped
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["entity_map"] == {
        "batterySoc": "sensor.new_soc",
        "gridPower": "sensor.grid",
    }


@pytest.mark.asyncio
async def test_options_flow_rejects_empty_map(hass: HomeAssistant, enable_custom_integrations) -> None:
    """Submitting with nothing selected re-shows the form with an error and
    leaves options untouched."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entity_map": {"batterySoc": "sensor.soc"}},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"batterySoc": "", "gridPower": ""},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_entities_selected"}
    assert entry.options == {}
```

- [ ] **Step 2: Run tests**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_config_flow.py::test_options_flow_saves_and_drops_blanks tests/test_config_flow.py::test_options_flow_rejects_empty_map -v`
Expected: PASS (the submit logic exists from Task 3). If `test_options_flow_saves_and_drops_blanks` fails because HA omits blank keys from `user_input` rather than passing `""`, that's fine — the `if eid` filter handles both; adjust the assertion's understanding, not the production code. If `test_options_flow_rejects_empty_map` fails, re-check the `errors["base"]` branch in `async_step_init`.

- [ ] **Step 3: Commit**

```bash
cd /Users/ivanursul/git/home-assistant-svitgrid/.worktrees/edit-sensor-mappings
git add tests/test_config_flow.py
git commit -m "test(config_flow): lock options-flow submit validation and persistence"
```

---

## Task 5: Runtime pickup — options-first read + reload-on-change listener

**Files:**
- Modify: `custom_components/svitgrid/__init__.py:193` (the `entity_map = ...` read) and `async_setup_entry` body (register listener) + add module-level `_async_reload_entry`
- Test: `tests/test_init.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_init.py`:

```python
@pytest.mark.asyncio
async def test_setup_prefers_options_entity_map(hass, enable_custom_integrations):
    """async_setup_entry uses entry.options['entity_map'] over entry.data's."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain="svitgrid",
        data={
            "api_base": "https://example.test",
            "api_key": "k",
            "edge_device_id": "dev1",
            "hardware_id": "hw1",
            "household_id": "hh1",
            "signing_key_id": "sk1",
            "private_key_pem": "pem",
            "public_key_hex": "ff",
            "trusted_keys": [],
            "entity_map": {"batterySoc": "sensor.from_data"},
        },
        options={"entity_map": {"batterySoc": "sensor.from_options"}},
    )
    entry.add_to_hass(hass)

    captured = {}

    async def _fake_loop(**kwargs):
        captured["entity_map"] = kwargs.get("entity_map")
        # Return immediately so the background task finishes.

    with patch("custom_components.svitgrid.run_readings_loop", _fake_loop), \
         patch("custom_components.svitgrid.run_command_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_mqtt_wake_loop", AsyncMock()):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    assert captured["entity_map"] == {"batterySoc": "sensor.from_options"}


@pytest.mark.asyncio
async def test_options_change_reloads_entry(hass, enable_custom_integrations):
    """Updating entry.options fires the update listener, reloading the entry."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain="svitgrid",
        data={
            "api_base": "https://example.test",
            "api_key": "k",
            "edge_device_id": "dev1",
            "hardware_id": "hw1",
            "household_id": "hh1",
            "signing_key_id": "sk1",
            "private_key_pem": "pem",
            "public_key_hex": "ff",
            "trusted_keys": [],
            "entity_map": {"batterySoc": "sensor.soc"},
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.svitgrid.run_readings_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_command_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_mqtt_wake_loop", AsyncMock()):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

        with patch.object(
            hass.config_entries, "async_reload", AsyncMock()
        ) as mock_reload:
            hass.config_entries.async_update_entry(
                entry, options={"entity_map": {"batterySoc": "sensor.new"}}
            )
            await hass.async_block_till_done()

    mock_reload.assert_called_once_with(entry.entry_id)
```

NOTE: match the patch targets (`run_readings_loop`, `run_command_loop`, `run_mqtt_wake_loop`) to the actual import names in `__init__.py`. Confirm with `grep -n "run_readings_loop\|run_command_loop\|run_mqtt_wake_loop\|import" custom_components/svitgrid/__init__.py` and copy the patterns used by the existing `test_async_setup_entry_starts_publisher_and_poller` test in this file (lines ~299-340) — reuse its exact patch targets and entry-data shape if they differ from above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_init.py::test_setup_prefers_options_entity_map tests/test_init.py::test_options_change_reloads_entry -v`
Expected: `test_setup_prefers_options_entity_map` FAILs (setup reads only `data`); `test_options_change_reloads_entry` FAILs (no listener registered → `async_reload` not called).

- [ ] **Step 3: Read the entity_map options-first**

In `custom_components/svitgrid/__init__.py`, change the line at ~193:

```python
    entity_map: dict[str, str] = dict(data.get("entity_map") or {})
```

to:

```python
    # Options (set by the edit/options flow) win over the pairing-time data,
    # so a user's edited mapping takes effect on the next reload.
    entity_map: dict[str, str] = dict(
        entry.options.get("entity_map") or data.get("entity_map") or {}
    )
```

- [ ] **Step 4: Register the reload-on-options-change listener**

In `async_setup_entry`, just before the final `return True`, add:

```python
    # Reload the entry whenever options change (e.g. the user edits the sensor
    # mappings) so the readings publisher restarts with the new entity_map.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
```

Add this module-level function near the other module-level helpers (e.g. directly after `_validate_entity_map`, ~line 47):

```python
async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener: reload the entry so a new entity_map takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest tests/test_init.py -v`
Expected: PASS (both new tests + all pre-existing init tests).

- [ ] **Step 6: Commit**

```bash
cd /Users/ivanursul/git/home-assistant-svitgrid/.worktrees/edit-sensor-mappings
git add custom_components/svitgrid/__init__.py tests/test_init.py
git commit -m "feat(init): read entity_map options-first and reload entry on options change"
```

---

## Task 6: Full suite + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full add-on test suite**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/pytest -q --timeout=60`
Expected: all tests pass. If any pre-existing test fails, confirm it also fails on `main` before this branch (note it as pre-existing, do not fix unrelated failures).

- [ ] **Step 2: Lint/parse sanity on touched files**

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/python -c "import json; json.load(open('custom_components/svitgrid/strings.json')); json.load(open('custom_components/svitgrid/translations/en.json')); print('json ok')"`
Expected: `json ok`

Run: `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ('custom_components/svitgrid/const.py','custom_components/svitgrid/config_flow.py','custom_components/svitgrid/__init__.py')]; print('py ok')"`
Expected: `py ok`

- [ ] **Step 3: Confirm no manifest/version change**

Run: `git diff main --stat custom_components/svitgrid/manifest.json`
Expected: empty (no release-triggering change in this branch, per the user-gated release rule).

---

## Notes for the implementer

- **Pre-fill must use `add_suggested_values_to_schema`**, never `vol.Optional(field, default=...)`. A voluptuous default re-inserts the old value when a field is submitted empty, making it impossible to *unmap* a sensor. Suggested values pre-fill the display without that stickiness.
- **`OptionsFlow` does not set `self.config_entry`** in this implementation — store `self._entry` to avoid the newer-HA reassignment warning.
- **`HomeAssistant` and `ConfigEntry` types** are already imported in `__init__.py` (used by `async_setup_entry`); reuse those imports for `_async_reload_entry`.
- **Do not push, do not bump `manifest.json`, do not create a release.** Releases are user-gated. The finishing step is a fast-forward merge into the add-on's local `main` only (the user decides when to push/release).
```
