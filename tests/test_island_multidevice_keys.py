"""Multi-device island keys: the add-on must accept a key from every paired
app instance, not just the most recent one."""

from __future__ import annotations

import pytest

from custom_components.svitgrid.keystore import KeystoreState


def _state(**overrides) -> KeystoreState:
    base = dict(
        api_key="ak",
        public_key_hex="04ff",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        signing_key_id="ha-1",
        trusted_key_ids=[],
        trusted_public_keys_hex={},
    )
    base.update(overrides)
    return KeystoreState(**base)


def test_all_island_keys_includes_legacy_scalar():
    """A box upgraded from the single-slot era keeps its existing key valid,
    so the device that works today does NOT break on add-on upgrade."""
    state = _state(island_key="legacy-key", island_keys={})
    assert state.all_island_keys() == ["legacy-key"]


def test_all_island_keys_merges_scalar_and_map():
    state = _state(
        island_key="legacy-key",
        island_keys={"phone": "phone-key", "tablet": "tablet-key"},
    )
    assert sorted(state.all_island_keys()) == [
        "legacy-key",
        "phone-key",
        "tablet-key",
    ]


def test_all_island_keys_empty_when_nothing_set():
    assert _state(island_key=None, island_keys={}).all_island_keys() == []


def test_all_island_keys_dedupes():
    """Re-running setup on the same device must not produce a duplicate."""
    state = _state(island_key="k", island_keys={"phone": "k"})
    assert state.all_island_keys() == ["k"]


class _FakeStore:
    """Stands in for HA's Store — async_load/async_save over a dict."""

    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


def _keystore(data):
    from custom_components.svitgrid.keystore import SvitgridKeystore

    ks = SvitgridKeystore.__new__(SvitgridKeystore)
    ks._store = _FakeStore(data)
    return ks


def _blob(**overrides):
    base = {
        "api_key": "ak",
        "public_key_hex": "04ff",
        "private_key_pem": "pem",
        "signing_key_id": "ha-1",
        "trusted_key_ids": [],
        "trusted_public_keys_hex": {},
        "island_key": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_load_defaults_island_keys_for_pre_upgrade_blob():
    """A stored blob written before this change has no `island_keys` key at
    all; load() must not KeyError."""
    ks = _keystore(_blob(island_key="old"))
    state = await ks.load()
    assert state.island_keys == {}
    assert state.all_island_keys() == ["old"]


@pytest.mark.asyncio
async def test_add_island_key_does_not_evict_another_device():
    """THE BUG: setting up island mode on a tablet must not revoke the phone."""
    ks = _keystore(_blob(island_keys={"phone": "phone-key"}))
    await ks.async_add_island_key("tablet", "tablet-key")
    keys = await ks.async_get_island_keys()
    assert "phone-key" in keys
    assert "tablet-key" in keys


@pytest.mark.asyncio
async def test_add_island_key_replaces_same_device():
    """Re-running setup on the SAME device rotates only that device's key."""
    ks = _keystore(_blob(island_keys={"phone": "old-key"}))
    await ks.async_add_island_key("phone", "new-key")
    keys = await ks.async_get_island_keys()
    assert keys == ["new-key"]


@pytest.mark.asyncio
async def test_get_island_keys_on_empty_store_returns_empty_list():
    ks = _keystore(None)
    assert await ks.async_get_island_keys() == []
