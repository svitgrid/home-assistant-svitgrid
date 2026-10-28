"""Round-trip keypair + API key through HA's Store. Uses the HA-in-process
fixture from pytest-homeassistant-custom-component."""

from __future__ import annotations

import pytest

from custom_components.svitgrid.keystore import SvitgridKeystore


@pytest.mark.asyncio
async def test_absent_returns_none(hass):
    ks = SvitgridKeystore(hass)
    assert await ks.load() is None


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(hass):
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair, public_key_to_hex

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="test-api-key",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="key-1",
        trusted_key_ids=["key-1"],
    )

    loaded = await ks.load()
    assert loaded is not None
    assert loaded.api_key == "test-api-key"
    assert loaded.public_key_hex == pub_hex
    assert loaded.signing_key_id == "key-1"
    assert loaded.trusted_key_ids == ["key-1"]
    # Private key can be re-constructed
    reloaded_priv = loaded.load_private_key()
    assert public_key_to_hex(reloaded_priv.public_key()) == pub_hex


@pytest.mark.asyncio
async def test_update_trusted_keys(hass):
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="s",
        trusted_key_ids=["a"],
    )
    await ks.update_trusted_keys(["a", "b"])
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.trusted_key_ids == ["a", "b"]


def _pem(priv):
    """Helper: serialize a private key to PEM bytes for testing."""
    from cryptography.hazmat.primitives import serialization

    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.mark.asyncio
async def test_trusted_public_keys_hex_roundtrip(hass):
    """KeystoreState gains a trusted_public_keys_hex dict that round-trips
    through HA Store alongside the existing trusted_key_ids list."""
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="sk",
        trusted_key_ids=["admin-1", "admin-2"],
        trusted_public_keys_hex={"admin-1": "04" + "aa" * 64, "admin-2": "04" + "bb" * 64},
    )
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.trusted_public_keys_hex == {
        "admin-1": "04" + "aa" * 64,
        "admin-2": "04" + "bb" * 64,
    }


@pytest.mark.asyncio
async def test_trusted_public_keys_hex_defaults_empty_when_missing(hass):
    """Legacy stored state (from v0.1.0) doesn't have trusted_public_keys_hex.
    Loading it should default to an empty dict, not raise."""
    from homeassistant.helpers.storage import Store

    from custom_components.svitgrid.const import STORAGE_KEY, STORAGE_VERSION

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save(
        {
            "api_key": "legacy-key",
            "public_key_hex": "04" + "cc" * 64,
            "private_key_pem": "dummy-pem",
            "signing_key_id": "legacy-sk",
            "trusted_key_ids": ["a", "b"],
            # NOTE: no trusted_public_keys_hex field
        }
    )

    ks = SvitgridKeystore(hass)
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.trusted_public_keys_hex == {}


@pytest.mark.asyncio
async def test_update_trusted_keys_hex_replaces_atomically(hass):
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="sk",
        trusted_key_ids=["a"],
        trusted_public_keys_hex={"a": "04" + "11" * 64},
    )
    await ks.update_trusted_keys_hex({"b": "04" + "22" * 64, "c": "04" + "33" * 64})
    loaded = await ks.load()
    assert loaded.trusted_public_keys_hex == {"b": "04" + "22" * 64, "c": "04" + "33" * 64}
    # Keys stay in sync with the ids list
    assert sorted(loaded.trusted_key_ids) == ["b", "c"]


# --- The edge box registry (edge island, Task 1) ---


async def _seeded(hass):
    from custom_components.svitgrid.signing import generate_keypair

    ks = SvitgridKeystore(hass)
    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="s",
        trusted_key_ids=["s"],
        trusted_public_keys_hex={"s": pub_hex},
    )
    return ks


def _box(device_id, api_key=None):
    from custom_components.svitgrid.keystore import EdgeBoxEntry

    return EdgeBoxEntry(
        device_id=device_id,
        api_key=api_key,
        inverter_ids=[f"inv-{device_id}"],
        inverters={f"inv-{device_id}": {"brand": "deye", "model": "sun-12k"}},
        added_at="2026-10-28T10:00:00.000Z",
        pair_code_hash=None,
        pair_code_expires_at=None,
    )


@pytest.mark.asyncio
async def test_edge_box_round_trips_through_save_and_load(hass):
    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1", api_key="edge-key-1"))
    # A later identity re-save (setup does one on every start) keeps the boxes.
    loaded = await ks.load()
    await ks.save(
        api_key=loaded.api_key,
        public_key_hex=loaded.public_key_hex,
        private_key_pem=loaded.private_key_pem,
        signing_key_id=loaded.signing_key_id,
        trusted_key_ids=loaded.trusted_key_ids,
        trusted_public_keys_hex=loaded.trusted_public_keys_hex,
    )

    reloaded = await SvitgridKeystore(hass).load()
    assert reloaded.edge_boxes == {"box-1": _box("box-1", api_key="edge-key-1")}
    assert reloaded.edge_boxes_list() == [_box("box-1", api_key="edge-key-1")]


@pytest.mark.asyncio
async def test_add_edge_box_replaces_the_same_device_id(hass):
    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1"))
    replacement = _box("box-1", api_key="edge-key-2")
    replacement.inverter_ids = ["inv-a", "inv-b"]
    await ks.async_add_edge_box(replacement)
    loaded = await ks.load()
    assert loaded.edge_boxes == {"box-1": replacement}


@pytest.mark.asyncio
async def test_edge_box_for_api_key_uses_constant_time_compare(hass, monkeypatch):
    import hmac

    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1", api_key="edge-key-1"))
    await ks.async_add_edge_box(_box("box-2", api_key="edge-key-2"))
    await ks.async_add_edge_box(_box("box-3"))  # unbound: never matches

    real = hmac.compare_digest
    calls = []

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    state = await ks.load()
    assert state.edge_box_for_api_key("edge-key-1").device_id == "box-1"
    # Every bound entry is compared, even after the first match.
    assert len(calls) == 2
    assert state.edge_box_for_api_key("edge-key-2").device_id == "box-2"
    assert state.edge_box_for_api_key("nope") is None
    assert state.edge_box_for_api_key("") is None


@pytest.mark.asyncio
async def test_remove_edge_box_is_idempotent(hass):
    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1"))
    await ks.async_add_edge_box(_box("box-2"))
    assert await ks.async_remove_edge_box("box-1") is True
    assert await ks.async_remove_edge_box("box-1") is False
    loaded = await ks.load()
    assert list(loaded.edge_boxes) == ["box-2"]


@pytest.mark.asyncio
async def test_load_without_edge_boxes_key_is_empty(hass):
    ks = await _seeded(hass)
    loaded = await ks.load()
    assert loaded.edge_boxes == {}
    assert loaded.edge_boxes_list() == []


@pytest.mark.asyncio
async def test_issue_pair_code_stores_only_a_hash(hass, hass_storage):
    import hashlib
    import json

    from custom_components.svitgrid.const import (
        PAIRING_CODE_ALPHABET,
        PAIRING_CODE_LENGTH,
        STORAGE_KEY,
    )

    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1"))
    code = await ks.async_issue_pair_code("box-1")

    assert len(code) == PAIRING_CODE_LENGTH
    assert set(code) <= set(PAIRING_CODE_ALPHABET)
    stored = hass_storage[STORAGE_KEY]["data"]
    assert code not in json.dumps(stored)
    entry = stored["edge_boxes"]["box-1"]
    assert entry["pair_code_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert entry["pair_code_expires_at"] is not None
    state = await ks.load()
    found, verdict = state.edge_box_for_pair_code(code)
    assert verdict == "ok"
    assert found.device_id == "box-1"
    assert state.edge_box_for_pair_code("AAAAAA") == (None, "invalid")


@pytest.mark.asyncio
async def test_pair_code_lookup_reports_expired(hass, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from custom_components.svitgrid import keystore as keystore_mod

    issued = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(keystore_mod, "_utcnow", lambda: issued)
    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1"))
    code = await ks.async_issue_pair_code("box-1")
    state = await ks.load()

    monkeypatch.setattr(keystore_mod, "_utcnow", lambda: issued + timedelta(seconds=599))
    assert state.edge_box_for_pair_code(code)[1] == "ok"
    monkeypatch.setattr(keystore_mod, "_utcnow", lambda: issued + timedelta(seconds=601))
    found, verdict = state.edge_box_for_pair_code(code)
    assert verdict == "expired"
    assert found is None


@pytest.mark.asyncio
async def test_bind_clears_the_code(hass):
    ks = await _seeded(hass)
    await ks.async_add_edge_box(_box("box-1"))
    code = await ks.async_issue_pair_code("box-1")
    await ks.async_bind_edge_box("box-1", "edge-key-1")

    state = await ks.load()
    entry = state.edge_boxes["box-1"]
    assert entry.api_key == "edge-key-1"
    assert entry.pair_code_hash is None
    assert entry.pair_code_expires_at is None
    # Single use: a bound box's code no longer matches.
    assert state.edge_box_for_pair_code(code) == (None, "invalid")
    assert state.edge_box_for_api_key("edge-key-1").device_id == "box-1"


@pytest.mark.asyncio
async def test_three_boxes_coexist(hass):
    ks = await _seeded(hass)
    codes = {}
    for did in ("box-1", "box-2", "box-3"):
        await ks.async_add_edge_box(_box(did))
        codes[did] = await ks.async_issue_pair_code(did)
    await ks.async_bind_edge_box("box-2", "edge-key-2")

    state = await ks.load()
    assert sorted(b.device_id for b in state.edge_boxes_list()) == ["box-1", "box-2", "box-3"]
    assert state.edge_box_for_pair_code(codes["box-1"])[0].device_id == "box-1"
    assert state.edge_box_for_pair_code(codes["box-3"])[0].device_id == "box-3"
    assert state.edge_box_for_api_key("edge-key-2").device_id == "box-2"
    assert await ks.async_remove_edge_box("box-1") is True
    state = await ks.load()
    assert sorted(state.edge_boxes) == ["box-2", "box-3"]


@pytest.mark.asyncio
async def test_keystore_written_by_0_25_0_loads(hass, hass_storage):
    """A keystore in the shape 0.25.0 writes (no `edge_boxes` key) loads, and a
    load and save leaves every field it had unchanged."""
    import copy

    from custom_components.svitgrid.const import STORAGE_KEY, STORAGE_VERSION
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    data_0_25_0 = {
        "api_key": "cloud-api-key",
        "public_key_hex": pub_hex,
        "private_key_pem": _pem(priv),
        "signing_key_id": "ha-signing-1",
        "trusted_key_ids": ["phone-key-1"],
        "trusted_public_keys_hex": {"phone-key-1": "04" + "ab" * 64},
        "island_key": "legacy-island-key",
        "island_keys": {
            "phone-1": {
                "key": "island-key-1",
                "label": "Pixel",
                "pairedAt": "2026-10-01T12:00:00.000Z",
            }
        },
        "key_devices": {"phone-key-1": "phone-1"},
        "revoked_key_ids": ["old-key"],
    }
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": copy.deepcopy(data_0_25_0),
    }

    ks = SvitgridKeystore(hass)
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.edge_boxes == {}
    await ks.save(
        api_key=loaded.api_key,
        public_key_hex=loaded.public_key_hex,
        private_key_pem=loaded.private_key_pem,
        signing_key_id=loaded.signing_key_id,
        trusted_key_ids=loaded.trusted_key_ids,
        trusted_public_keys_hex=loaded.trusted_public_keys_hex,
    )

    assert STORAGE_VERSION == 1
    assert hass_storage[STORAGE_KEY]["version"] == 1
    saved = hass_storage[STORAGE_KEY]["data"]
    assert {k: saved[k] for k in data_0_25_0} == data_0_25_0
    assert saved["edge_boxes"] == {}
