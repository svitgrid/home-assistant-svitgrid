"""Multi-device island keys: the add-on must accept a key from every paired
app instance, not just the most recent one.

Import note: importing `custom_components.svitgrid.keystore` normally
triggers `custom_components/svitgrid/__init__.py` -> `.http_views` -> `.panel`
-> `homeassistant.components.http.StaticPathConfig`, which doesn't exist on
this env's pinned homeassistant (pre-existing, documented collection
failure — see other test files' `_load_views`/`_load_keystore` helpers). So
`keystore.py` is loaded here by file path via importlib, with its sole
sibling dependency (`const.py`) pre-registered in `sys.modules` under its
expected dotted name so `from .const import ...` resolves without importing
the real package `__init__.py`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "svitgrid")


def _load(mod_name: str, path: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_keystore():
    """Load keystore.py, working around the panel-import collection failure.

    Try the plain package import first (works if the environment's HA
    package happens to be compatible); fall back to file-path loading with
    `const` pre-injected into sys.modules under
    `custom_components.svitgrid.const` so `from .const import ...` resolves
    without executing the real (broken-in-this-env) package `__init__.py`.
    """
    try:
        import custom_components.svitgrid.keystore as ks

        return ks
    except ImportError:
        pass

    pkg_name = "custom_components.svitgrid"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [BASE]
        sys.modules["custom_components"] = types.ModuleType("custom_components")
        sys.modules["custom_components"].__path__ = [os.path.join(BASE, "..")]
        sys.modules[pkg_name] = pkg

    for name in ("const",):
        full_name = f"{pkg_name}.{name}"
        if full_name not in sys.modules:
            sys.modules[full_name] = _load(full_name, os.path.join(BASE, f"{name}.py"))

    return _load(f"{pkg_name}.keystore", os.path.join(BASE, "keystore.py"))


_keystore_module = _load_keystore()
KeystoreState = _keystore_module.KeystoreState


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
        island_keys={
            "phone": {"key": "phone-key", "label": None, "pairedAt": None},
            "tablet": {"key": "tablet-key", "label": None, "pairedAt": None},
        },
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
    state = _state(
        island_key="k", island_keys={"phone": {"key": "k", "label": None, "pairedAt": None}}
    )
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
    SvitgridKeystore = _keystore_module.SvitgridKeystore

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


from custom_components.svitgrid.island_auth import island_key_present_and_valid


class _FakeRequest(dict):
    """aiohttp Request stand-in: a dict (for KEY_AUTHENTICATED) with headers."""

    def __init__(self, headers=None, authenticated=False):
        super().__init__()
        self.headers = headers or {}
        if authenticated:
            from homeassistant.helpers.http import KEY_AUTHENTICATED

            self[KEY_AUTHENTICATED] = True


def test_accepts_any_key_in_the_list():
    req = _FakeRequest({"X-Island-Key": "tablet-key"})
    assert island_key_present_and_valid(req, ["phone-key", "tablet-key"]) is True


def test_accepts_the_first_key_too():
    req = _FakeRequest({"X-Island-Key": "phone-key"})
    assert island_key_present_and_valid(req, ["phone-key", "tablet-key"]) is True


def test_rejects_a_key_that_is_not_registered():
    req = _FakeRequest({"X-Island-Key": "revoked-key"})
    assert island_key_present_and_valid(req, ["phone-key", "tablet-key"]) is False


def test_rejects_when_no_keys_registered():
    req = _FakeRequest({"X-Island-Key": "anything"})
    assert island_key_present_and_valid(req, []) is False


def test_rejects_when_header_absent():
    assert island_key_present_and_valid(_FakeRequest({}), ["phone-key"]) is False


def test_still_accepts_a_bare_string_for_backward_compat():
    """Call sites not yet migrated to the list form must keep working."""
    req = _FakeRequest({"X-Island-Key": "solo"})
    assert island_key_present_and_valid(req, "solo") is True
    assert island_key_present_and_valid(req, None) is False


@pytest.mark.asyncio
async def test_enable_island_without_device_id_uses_legacy_bucket():
    """An OLD app sends no deviceId.  It must still work — bucketed under a
    fixed key so repeated setups from old apps reuse one slot (matching the
    old single-slot behaviour) without disturbing new-app devices."""
    ks = _keystore(_blob(island_keys={"tablet": "tablet-key"}))
    await ks.async_add_island_key("legacy", "old-app-key")
    keys = await ks.async_get_island_keys()
    assert "tablet-key" in keys
    assert "old-app-key" in keys


@pytest.mark.asyncio
async def test_repeated_legacy_setups_do_not_accumulate():
    ks = _keystore(_blob())
    await ks.async_add_island_key("legacy", "first")
    await ks.async_add_island_key("legacy", "second")
    assert await ks.async_get_island_keys() == ["second"]


# ---------------------------------------------------------------------------
# Hostile/malformed `deviceId` in the enable_island command payload
#
# `appDeviceId` is unvalidated end-to-end (API hand-parses the body; a plan
# step meant to add zod validation there never landed). A dict/list `deviceId`
# used directly as a dict key raises `TypeError: unhashable type` in
# `async_add_island_key`'s `{**current.island_keys, device_id: key}` — taking
# down the command-poller loop for the whole add-on. An int silently "works"
# in memory but gets stringified by `json.dump` on persist, so the key is
# unreachable after restart. The handler must coerce anything that isn't a
# non-empty string down to the same "legacy" bucket used when deviceId is
# absent, matching `command_poller.py`'s existing `payload.get("deviceId")
# or "legacy"` fallback for the *missing* case.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from custom_components.svitgrid.command_poller import process_command  # noqa: E402
from custom_components.svitgrid.signing import generate_keypair  # noqa: E402


def _make_api_client() -> MagicMock:
    c = MagicMock()
    c.ack_command = AsyncMock()
    return c


def _make_hass_entry(entry_data: dict | None = None):
    hass = MagicMock()
    hass.is_stopping = False
    hass.config_entries = MagicMock()
    hass.async_create_task = MagicMock()

    entry = MagicMock()
    entry.data = entry_data if entry_data is not None else {"cloud_ingest_enabled": True}
    entry.entry_id = "e1"
    return hass, entry


@pytest.mark.parametrize(
    "hostile_device_id",
    [
        {"a": 1},
        ["x"],
        123,
        True,
    ],
    ids=["dict", "list", "int", "bool"],
)
@pytest.mark.asyncio
async def test_enable_island_with_non_string_device_id_falls_back_to_legacy(
    hostile_device_id,
):
    """THE BUG: a non-string deviceId (object/array/number/bool) must not be
    used as a dict key — that either raises TypeError (dict/list, unhashable)
    or silently corrupts the store on persist (int/bool, stringified by
    json.dump). The handler must treat it the same as a missing deviceId and
    bucket the key under "legacy" instead of crashing."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    ks = _keystore(_blob(island_keys={}))
    hass, entry = _make_hass_entry()

    await process_command(
        command={
            "commandId": "c-hostile",
            "command": "enable_island",
            "payload": {
                "islandKey": "hostile-key",
                "deviceId": hostile_device_id,
                "cloudIngest": False,
            },
        },
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=ks,
        hass=hass,
        entry=entry,
    )

    keys = await ks.async_get_island_keys()
    assert "hostile-key" in keys
    stored = await ks._store.async_load()
    assert list(stored["island_keys"].keys()) == ["legacy"]
