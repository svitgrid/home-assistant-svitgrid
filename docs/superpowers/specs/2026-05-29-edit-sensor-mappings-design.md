# Edit Sensor Mappings — Design

**Date:** 2026-05-29
**Status:** Approved (post-brainstorm, pre-plan)
**Scope:** Home Assistant add-on only (`custom_components/svitgrid`). No API / server changes.

## Problem

The `entity_map` (Svitgrid reading field → Home Assistant `sensor.*` entity) is captured
**once at pairing time** and never editable afterward:

- Preset mode: seeded from the server preset returned by `/finalize`.
- Manual mode: hand-picked in the config flow's `manual_entities` step (`_MANUAL_FIELDS`).

It is stored in `entry.data["entity_map"]`, read once in `async_setup_entry`, and handed to
the readings publisher, which loops over it every interval to build the reading payload.

Today the only way to change a mapping is to delete the integration and re-pair. There is no
options flow (no **Configure** button on the integration card) and no entry-reload listener.

## Goal

Add a Home Assistant **Options Flow** so a paired user can change which HA sensor feeds each
Svitgrid field, after pairing, and have the running publisher pick up the change automatically.
Local only — the change never leaves Home Assistant.

## Non-goals

- **No server sync.** The server only ever seeded the map at pairing. Edits stay local; the
  publisher consumes the local `entity_map`. No API endpoint, no new client call.
- **No metadata editing.** Brand / model / phases / `has_battery` / `pv_strings` are not exposed.
  They have no local runtime effect (the publisher only iterates `entity_map`).
- **No 6-required-field enforcement.** The stricter YAML `_validate_entity_map` rule
  (`REQUIRED_FIELDS`) is not applied here — it would risk locking out validly-paired devices
  that never had all 6 mapped. The interactive rule (≥1 entity) is used instead.

## Existing-code discrepancy this design resolves

The mappable-field list is duplicated and already drifted:

- `const.ALL_FIELDS` ≈ 20 fields (the full set the publisher supports).
- `config_flow._MANUAL_FIELDS` = a separate hardcoded list of 13 fields (omits e.g.
  `batteryCurrent`, `batteryTemperature`, `gridFrequency`, daily energy counters,
  `inverterTemperature`).

Validation also disagrees between the two existing entry paths:

- YAML config path (`CONFIG_SCHEMA` → `_validate_entity_map`) requires all 6 `REQUIRED_FIELDS`.
- Interactive pairing (`manual_entities`) requires only **≥1** entity.

This design introduces a single ordered source of truth and aligns the manual flow to it.

## Design

### 1. Shared field definition (`const.py`)

Add one ordered list of `(field, human_label)` tuples — `MAPPABLE_FIELDS` — covering every
field in `ALL_FIELDS`, grouped sensibly (battery → PV strings → grid → load → daily energy →
temperatures/frequency). This becomes the single source of truth for "which fields can be
mapped, and what do we call them."

Refactor `config_flow._MANUAL_FIELDS` to derive from `MAPPABLE_FIELDS` (same field set + labels)
so the pairing flow and the new edit flow can never drift again. A guard test locks this in.

### 2. Options flow (`config_flow.py`)

- Add a `@staticmethod async_get_options_flow(config_entry)` to `SvitgridConfigFlow`, returning
  a new `SvitgridOptionsFlow(config_entries.OptionsFlow)`.
- Single step `async_step_init`:
  - Builds a form with one `EntitySelector(EntitySelectorConfig(domain="sensor"))` per
    `MAPPABLE_FIELDS` entry, each marked `vol.Optional(field, default=<current value>)` so it is
    **pre-filled** with the current mapping.
  - Current map precedence: `entry.options.get("entity_map") or entry.data.get("entity_map") or {}`.
  - On submit: build `cleaned = {field: eid for field, eid in user_input.items() if eid}`
    (clearing a selector unmaps that field). If `cleaned` is empty, re-show the form with
    `errors["base"] = "no_entities_selected"` (reuse the existing key). Otherwise persist with
    `self.async_create_entry(title="", data={"entity_map": cleaned})` — this writes to
    `entry.options`.

### 3. Runtime pickup (`__init__.py`)

- In `async_setup_entry`, read the map options-first:
  `entity_map = dict(entry.options.get("entity_map") or entry.data.get("entity_map") or {})`.
- Register an update listener so saving options restarts the publisher with the new map:
  `entry.async_on_unload(entry.add_update_listener(_async_reload_entry))`, where
  `_async_reload_entry` calls `await hass.config_entries.async_reload(entry.entry_id)`.
  No update listener exists today; this is new.

### 4. Strings (`strings.json` + `translations/en.json`)

Add an `options` section: `step.init` title + description, and `data` labels for each field,
sourced from the `MAPPABLE_FIELDS` labels. Add the `no_entities_selected` error under
`options.error` if HA does not fall back to the config-flow error namespace.

## Data flow

```
User clicks "Configure" on the Svitgrid integration
  → OptionsFlow.async_step_init renders a per-field EntitySelector form,
    pre-filled from entry.options ?? entry.data
  → User edits / clears selectors, submits
  → ≥1 entity validated; cleaned map written to entry.options["entity_map"]
  → update listener fires → async_reload(entry)
  → async_setup_entry re-reads entity_map (options-first) → publisher restarts
  → next interval, readings are built from the new mapping
```

## Error handling

- **Empty map on submit:** re-show form with `no_entities_selected`; nothing persisted.
- **Cleared individual field:** simply absent from the new map — that field stops publishing.
- **Reload failure:** delegated to Home Assistant's standard config-entry reload error handling.

## Testing (pytest, TDD)

- `async_get_options_flow` returns the options flow for an entry.
- `async_step_init` (no input) renders a form pre-filled from the current map, with
  options taking precedence over data.
- Submit drops blank selectors and writes the cleaned map to `entry.options["entity_map"]`.
- Submit with everything cleared → `no_entities_selected`, no write.
- Saving options triggers an entry reload (update listener wired).
- Guard: the options field list and `config_flow._MANUAL_FIELDS` both derive from
  `const.MAPPABLE_FIELDS` (single source of truth — no drift).

## Rollout

Pure add-on change. Merge to the add-on `main` (no manifest bump / no release tag in this change,
matching the user-gated release rule). A release that ships it to users via HACS is a separate,
user-gated step.
