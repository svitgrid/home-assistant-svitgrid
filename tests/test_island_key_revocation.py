"""Island key revocation: roster + per-device removal.

0.16.0 made island keys per-device but never removable, so every device that
ever paired kept LAN access permanently.

Import note: same file-path-loading workaround as
`tests/test_island_multidevice_keys.py` (read that file first) — importing
`custom_components.svitgrid.keystore` normally triggers
`custom_components/svitgrid/__init__.py` -> `.http_views` -> `.panel` ->
`homeassistant.components.http.StaticPathConfig`, which doesn't exist on this
env's pinned homeassistant (pre-existing, documented collection failure).
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


def _state(**overrides):
    base = dict(
        api_key="ak",
        public_key_hex="04ff",
        private_key_pem="pem",
        signing_key_id="ha-1",
        trusted_key_ids=[],
        trusted_public_keys_hex={},
    )
    base.update(overrides)
    return KeystoreState(**base)


def test_all_island_keys_reads_key_from_entry():
    state = _state(island_keys={"phone": {"key": "k1", "label": "Pixel 7", "pairedAt": None}})
    assert state.all_island_keys() == ["k1"]


def test_all_island_keys_still_merges_legacy_scalar():
    state = _state(
        island_key="old",
        island_keys={"phone": {"key": "k1", "label": None, "pairedAt": None}},
    )
    assert sorted(state.all_island_keys()) == ["k1", "old"]


def test_all_island_keys_skips_malformed_entry():
    """A malformed entry must not authorize and must not raise."""
    state = _state(island_keys={"bad": {"label": "no key here"}})
    assert state.all_island_keys() == []


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
async def test_load_migrates_bare_string_entries_from_0_16_0():
    """0.16.0 shipped `deviceId -> key` as a bare string and is already
    released, so a real box in the field has this shape."""
    ks = _keystore(_blob(island_keys={"phone": "k1"}))
    state = await ks.load()
    assert state.island_keys == {"phone": {"key": "k1", "label": None, "pairedAt": None}}
    assert state.all_island_keys() == ["k1"]


@pytest.mark.asyncio
async def test_load_preserves_rich_entries():
    ks = _keystore(
        _blob(
            island_keys={
                "phone": {"key": "k1", "label": "Pixel 7", "pairedAt": "2026-07-20T06:00:00Z"}
            }
        )
    )
    state = await ks.load()
    assert state.island_keys["phone"]["label"] == "Pixel 7"


@pytest.mark.asyncio
async def test_add_island_key_stores_label_and_paired_at():
    ks = _keystore(_blob())
    await ks.async_add_island_key("phone", "k1", label="Pixel 7", paired_at="2026-07-20T06:00:00Z")
    state = await ks.load()
    assert state.island_keys["phone"] == {
        "key": "k1",
        "label": "Pixel 7",
        "pairedAt": "2026-07-20T06:00:00Z",
    }


@pytest.mark.asyncio
async def test_add_island_key_without_label_still_works():
    """An older app sends no label; the entry must still be created."""
    ks = _keystore(_blob())
    await ks.async_add_island_key("phone", "k1")
    state = await ks.load()
    assert state.island_keys["phone"]["key"] == "k1"
    assert state.island_keys["phone"]["label"] is None


@pytest.mark.asyncio
async def test_revoke_removes_only_the_named_device():
    ks = _keystore(
        _blob(
            island_keys={
                "phone": {"key": "k1", "label": None, "pairedAt": None},
                "tablet": {"key": "k2", "label": None, "pairedAt": None},
            }
        )
    )
    removed = await ks.async_revoke_island_key("phone")
    assert removed is True
    assert await ks.async_get_island_keys() == ["k2"]


@pytest.mark.asyncio
async def test_revoke_legacy_clears_the_scalar_only():
    ks = _keystore(
        _blob(island_key="old", island_keys={"phone": {"key": "k1", "label": None, "pairedAt": None}})
    )
    removed = await ks.async_revoke_island_key("__legacy__")
    assert removed is True
    state = await ks.load()
    assert state.island_key is None
    assert state.island_keys["phone"]["key"] == "k1"


@pytest.mark.asyncio
async def test_revoke_is_idempotent():
    """A double-tap or retry must not be an error."""
    ks = _keystore(_blob(island_keys={"phone": {"key": "k1", "label": None, "pairedAt": None}}))
    assert await ks.async_revoke_island_key("phone") is True
    assert await ks.async_revoke_island_key("phone") is False
    assert await ks.async_get_island_keys() == []


@pytest.mark.asyncio
async def test_list_island_devices_never_exposes_a_key():
    """THE critical assertion: a roster that leaks the secrets it describes
    would be worse than the gap this feature closes."""
    ks = _keystore(
        _blob(
            island_key="old-secret",
            island_keys={
                "phone": {
                    "key": "super-secret",
                    "label": "Pixel 7",
                    "pairedAt": "2026-07-20T06:00:00Z",
                },
            },
        )
    )
    devices = await ks.async_list_island_devices()
    blob = repr(devices)
    assert "super-secret" not in blob
    assert "old-secret" not in blob
    assert {d["deviceId"] for d in devices} == {"phone", "__legacy__"}
    phone = next(d for d in devices if d["deviceId"] == "phone")
    assert phone == {
        "deviceId": "phone",
        "label": "Pixel 7",
        "pairedAt": "2026-07-20T06:00:00Z",
        "isLegacy": False,
    }


@pytest.mark.asyncio
async def test_list_omits_legacy_row_when_no_scalar():
    ks = _keystore(_blob(island_keys={"phone": {"key": "k1", "label": None, "pairedAt": None}}))
    devices = await ks.async_list_island_devices()
    assert [d["deviceId"] for d in devices] == ["phone"]


# ---------------------------------------------------------------------------
# enable_island: records the device label + paired-at, and rejects the
# reserved `__legacy__` id so a crafted client can't hide behind the
# synthetic roster row and become un-revocable.
#
# Harness mirrors `test_island_multidevice_keys.py`'s
# `test_enable_island_on_second_device_does_not_evict_first_devices_key` —
# drives the poller's real `process_command` rather than reimplementing its
# logic.
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


async def _run_enable_island(keystore, payload):
    """Thin helper: invokes the poller's real `process_command` with an
    `enable_island` command carrying `payload`."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    hass, entry = _make_hass_entry()

    await process_command(
        command={
            "commandId": "c-1",
            "command": "enable_island",
            "payload": payload,
        },
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=keystore,
        hass=hass,
        entry=entry,
    )


@pytest.mark.asyncio
async def test_enable_island_stores_the_device_label():
    ks = _keystore(_blob())
    await _run_enable_island(ks, {"islandKey": "k1", "deviceId": "phone", "deviceLabel": "Pixel 7"})
    state = await ks.load()
    assert state.island_keys["phone"]["label"] == "Pixel 7"
    assert state.island_keys["phone"]["pairedAt"] is not None


@pytest.mark.asyncio
async def test_enable_island_without_label_still_pairs():
    ks = _keystore(_blob())
    await _run_enable_island(ks, {"islandKey": "k1", "deviceId": "phone"})
    state = await ks.load()
    assert state.island_keys["phone"]["key"] == "k1"
    assert state.island_keys["phone"]["label"] is None


@pytest.mark.asyncio
async def test_enable_island_rejects_the_reserved_legacy_id():
    """A crafted client must not be able to pair AS the synthetic legacy row —
    that would let it hide there and become un-revocable."""
    ks = _keystore(_blob())
    await _run_enable_island(ks, {"islandKey": "k1", "deviceId": "__legacy__"})
    state = await ks.load()
    assert "__legacy__" not in state.island_keys
    assert state.island_keys["legacy"]["key"] == "k1"


@pytest.mark.asyncio
async def test_enable_island_ignores_a_non_string_label():
    ks = _keystore(_blob())
    await _run_enable_island(ks, {"islandKey": "k1", "deviceId": "phone", "deviceLabel": {"a": 1}})
    state = await ks.load()
    assert state.island_keys["phone"]["label"] is None
