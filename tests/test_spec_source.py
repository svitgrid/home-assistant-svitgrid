"""Where a direct-harvest inverter's register spec comes from.

Before this, setup fetched the spec from the cloud on every start and never
kept it, so a Home Assistant that started without the cloud polled nothing.
The order now is: the cloud (unless the entry is local-only), then the last
spec the cloud gave and this install saved, then the copy bundled with the
component.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.harvest.spec_source import (
    RegisterSpecStore,
    load_bundled_spec,
    resolve_register_spec,
)

MODEL = "deye_sg04lp3"


def _spec(version: int, marker: str = "cloud") -> dict:
    return {
        "modelId": MODEL,
        "version": version,
        "protocol": "solarman_v5",
        "reads": [],
        "derivations": [],
        "marker": marker,
    }


@pytest.mark.asyncio
async def test_a_cloud_spec_is_returned_and_saved(hass):
    store = RegisterSpecStore(hass)
    fetch = AsyncMock(return_value=_spec(1))

    spec = await resolve_register_spec(hass, MODEL, fetch=fetch, store=store)

    assert spec["marker"] == "cloud"
    assert (await store.async_get(MODEL))["marker"] == "cloud"
    fetch.assert_awaited_once_with(MODEL)


@pytest.mark.asyncio
async def test_a_saved_spec_survives_a_new_store_instance(hass):
    """It is saved to Home Assistant storage, not kept in memory."""
    await RegisterSpecStore(hass).async_put(MODEL, _spec(2, "saved"))
    assert (await RegisterSpecStore(hass).async_get(MODEL))["marker"] == "saved"


@pytest.mark.asyncio
async def test_an_unreachable_cloud_falls_back_to_the_saved_spec(hass):
    store = RegisterSpecStore(hass)
    await store.async_put(MODEL, _spec(1, "saved"))
    fetch = AsyncMock(side_effect=RuntimeError("no route to host"))

    spec = await resolve_register_spec(hass, MODEL, fetch=fetch, store=store)

    assert spec["marker"] == "saved"


@pytest.mark.asyncio
async def test_with_nothing_saved_an_unreachable_cloud_falls_back_to_the_bundle(hass):
    fetch = AsyncMock(side_effect=RuntimeError("no route to host"))

    spec = await resolve_register_spec(hass, MODEL, fetch=fetch, store=RegisterSpecStore(hass))

    assert spec == load_bundled_spec(MODEL)


@pytest.mark.asyncio
async def test_a_cloud_404_falls_back_to_the_bundle(hass):
    fetch = AsyncMock(return_value=None)

    spec = await resolve_register_spec(hass, MODEL, fetch=fetch, store=RegisterSpecStore(hass))

    assert spec == load_bundled_spec(MODEL)


@pytest.mark.asyncio
async def test_a_local_only_entry_never_asks_the_cloud(hass):
    """fetch=None is a local-only entry: saved first, then the bundle."""
    spec = await resolve_register_spec(hass, MODEL, fetch=None, store=RegisterSpecStore(hass))
    assert spec == load_bundled_spec(MODEL)


@pytest.mark.asyncio
async def test_offline_the_saved_spec_beats_the_bundle_whatever_their_versions(hass):
    """Specs change without a version bump, so versions cannot say which copy
    is newer. The saved copy came from the cloud more recently than the
    bundle was made."""
    store = RegisterSpecStore(hass)
    await store.async_put(MODEL, _spec(0, "saved"))

    spec = await resolve_register_spec(hass, MODEL, fetch=None, store=store)

    assert spec["marker"] == "saved"


@pytest.mark.asyncio
async def test_a_cloud_fix_without_a_version_bump_replaces_the_saved_spec(hass):
    """Regression: every monorepo spec is version 1 and maps are fixed in
    place. Taking the cloud copy only when its version was strictly higher
    froze every cloud install on the first copy it saved."""
    store = RegisterSpecStore(hass)
    await store.async_put(MODEL, _spec(1, "old map"))
    fetch = AsyncMock(return_value=_spec(1, "fixed map"))

    spec = await resolve_register_spec(hass, MODEL, fetch=fetch, store=store)

    assert spec["marker"] == "fixed map"
    assert (await store.async_get(MODEL))["marker"] == "fixed map"


@pytest.mark.asyncio
async def test_the_cloud_wins_over_the_bundle_when_it_answers(hass):
    fetch = AsyncMock(return_value=_spec(1, "cloud"))

    spec = await resolve_register_spec(hass, MODEL, fetch=fetch, store=RegisterSpecStore(hass))

    assert spec["marker"] == "cloud"


@pytest.mark.asyncio
async def test_an_unknown_model_with_no_cloud_is_none(hass):
    spec = await resolve_register_spec(
        hass, "no_such_model", fetch=None, store=RegisterSpecStore(hass)
    )
    assert spec is None
