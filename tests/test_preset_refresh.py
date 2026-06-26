"""Tests for preset_refresh — add-only entity_map merge, version gate, async orchestration."""

import pytest

from custom_components.svitgrid.preset_refresh import (
    merge_entity_map,
    refresh_entry_inverters,
    should_merge,
)


# ---------------------------------------------------------------------------
# merge_entity_map
# ---------------------------------------------------------------------------


def test_merge_adds_only_missing_keys():
    cur = {"pv1Power": "sensor.a", "gridPower": "sensor.b"}
    preset = {"pv1Power": "sensor.DIFFERENT", "dailyLossesEnergy": "sensor.loss", "loadFrequency": "sensor.lf"}
    merged, added = merge_entity_map(cur, preset)
    assert merged["pv1Power"] == "sensor.a"          # existing NOT overwritten
    assert merged["dailyLossesEnergy"] == "sensor.loss"  # new added
    assert merged["loadFrequency"] == "sensor.lf"
    assert set(added) == {"dailyLossesEnergy", "loadFrequency"}
    assert merged["gridPower"] == "sensor.b"          # existing key kept (no removal)


def test_merge_noop_returns_empty_added():
    cur = {"pv1Power": "sensor.a"}
    merged, added = merge_entity_map(cur, {"pv1Power": "sensor.z"})
    assert added == []
    assert merged == {"pv1Power": "sensor.a"}


# ---------------------------------------------------------------------------
# should_merge
# ---------------------------------------------------------------------------


def test_should_merge_version_gate():
    assert should_merge("6", 0) is True      # missing/0 stored -> catch up
    assert should_merge("6", "5") is True    # newer
    assert should_merge("6", "6") is False   # equal -> skip
    assert should_merge("5", "6") is False   # older -> skip
    assert should_merge(6, None) is True     # missing stored -> merge


# ---------------------------------------------------------------------------
# refresh_entry_inverters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_merges_and_records_version():
    invs = [{"inverterId": "i1", "preset_id": "p1", "entity_map": {"pv1Power": "s.a"}}]

    async def fetch(pid):
        assert pid == "p1"
        return {"version": "6", "entityMap": {"pv1Power": "s.z", "dailyLossesEnergy": "s.loss"}}

    out, changed = await refresh_entry_inverters(invs, fetch, log=lambda *_: None)
    assert changed is True
    assert out[0]["entity_map"]["dailyLossesEnergy"] == "s.loss"
    assert out[0]["entity_map"]["pv1Power"] == "s.a"   # not overwritten
    assert str(out[0]["merged_preset_version"]) == "6"


@pytest.mark.asyncio
async def test_refresh_skips_manual_mode_and_failopen():
    invs = [
        {"inverterId": "manual", "entity_map": {"pv1Power": "s.a"}},          # no preset_id
        {"inverterId": "err", "preset_id": "p", "entity_map": {}},
    ]

    async def fetch(pid):
        raise RuntimeError("offline")

    out, changed = await refresh_entry_inverters(invs, fetch, log=lambda *_: None)
    assert changed is False
    assert out == invs   # untouched


@pytest.mark.asyncio
async def test_refresh_noop_when_version_not_newer():
    invs = [{"inverterId": "i1", "preset_id": "p1", "entity_map": {"pv1Power": "s.a"}, "merged_preset_version": "6"}]

    async def fetch(pid):
        return {"version": "6", "entityMap": {"dailyLossesEnergy": "s.loss"}}

    out, changed = await refresh_entry_inverters(invs, fetch, log=lambda *_: None)
    assert changed is False
    assert "dailyLossesEnergy" not in out[0]["entity_map"]
