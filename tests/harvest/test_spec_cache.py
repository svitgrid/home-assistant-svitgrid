import pytest

from custom_components.svitgrid.harvest.spec_cache import load_spec


@pytest.mark.asyncio
async def test_first_load_changes():
    async def fetch(_m): return {"modelId": "m", "version": 2}
    spec, changed = await load_spec(fetch, "m", cached=None)
    assert changed and spec["version"] == 2


@pytest.mark.asyncio
async def test_same_version_no_change_keeps_cached():
    cached = {"modelId": "m", "version": 2}
    async def fetch(_m): return {"modelId": "m", "version": 2}
    spec, changed = await load_spec(fetch, "m", cached=cached)
    assert not changed and spec is cached


@pytest.mark.asyncio
async def test_fetch_failure_keeps_cached():
    cached = {"modelId": "m", "version": 1}
    async def fetch(_m): raise RuntimeError("net")
    spec, changed = await load_spec(fetch, "m", cached=cached)
    assert not changed and spec is cached
