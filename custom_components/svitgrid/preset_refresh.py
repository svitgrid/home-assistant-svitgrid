"""Preset entity-map refresh — add-only merge, version gate, async orchestration."""

from __future__ import annotations


def merge_entity_map(current: dict, preset_map: dict) -> tuple[dict, list[str]]:
    """Return (merged, added_keys) where only keys absent from current are copied in.

    Never overwrites or removes existing keys. Pure — does not mutate inputs.
    """
    added = [k for k in preset_map if k not in current]
    if not added:
        return current, []
    merged = dict(current)
    for k in added:
        merged[k] = preset_map[k]
    return merged, added


def _as_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def should_merge(preset_version, stored_version) -> bool:
    """Return True when preset_version is strictly greater than stored_version.

    Numeric comparison when both parse as float; string comparison otherwise.
    stored_version=None is treated as 0 so the first real preset always merges.
    """
    if stored_version is None:
        stored_version = 0
    pn, sn = _as_num(preset_version), _as_num(stored_version)
    if pn is not None and sn is not None:
        return pn > sn
    return str(preset_version) > str(stored_version)


async def refresh_entry_inverters(
    inverters: list[dict],
    fetch_preset,
    log,
) -> tuple[list[dict], bool]:
    """For each inverter with a preset_id, fetch the preset and add-only merge.

    - Skips manual-mode inverters (no preset_id).
    - Fail-open: any exception or None preset → inverter unchanged, never raises.
    - Returns (updated_inverters, changed).
    """
    changed = False
    out = []
    for inv in inverters:
        new_inv = dict(inv)
        pid = inv.get("preset_id")
        if pid:
            try:
                preset = await fetch_preset(pid)
                if preset and should_merge(preset.get("version"), inv.get("merged_preset_version")):
                    merged, added = merge_entity_map(
                        inv.get("entity_map") or {},
                        preset.get("entityMap") or {},
                    )
                    if added:
                        new_inv["entity_map"] = merged
                        new_inv["merged_preset_version"] = preset.get("version")
                        changed = True
                        log(
                            f"added {len(added)} field mappings from preset {pid} "
                            f"v{preset.get('version')}: {','.join(added)}"
                        )
                    elif new_inv.get("merged_preset_version") != preset.get("version"):
                        # version advanced but nothing new to add — record so we don't re-merge each boot
                        new_inv["merged_preset_version"] = preset.get("version")
                        changed = True
            except Exception as err:  # fail-open
                log(f"preset refresh skipped for {pid}: {err}")
        out.append(new_inv)
    return out, changed
