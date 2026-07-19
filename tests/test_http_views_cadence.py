"""TDD tests for GET/PUT /api/svitgrid/cadence (Task 2 — island cadence endpoint).

Written BEFORE implementation (RED phase).
"""

from __future__ import annotations

import json

import pytest

from custom_components.svitgrid.const import DOMAIN

ISLAND_KEY = "test-island-key-cadence"


# ---------------------------------------------------------------------------
# Fakes (mirror test_http_views_island_auth.py)
# ---------------------------------------------------------------------------


class _FakeKeystore:
    def __init__(self, island_key: str | None) -> None:
        self._island_key = island_key

    async def async_get_island_key(self) -> str | None:
        return self._island_key

    async def async_get_island_keys(self) -> list[str]:
        return [self._island_key] if self._island_key else []


class _FakeHeaders(dict):
    """Case-insensitive header dict matching aiohttp CIMultiDictProxy semantics."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeConfigEntry:
    """Minimal config entry stub."""

    def __init__(self, entry_id: str, data: dict) -> None:
        self.entry_id = entry_id
        self.data = dict(data)


class _FakeConfigEntries:
    """Minimal hass.config_entries stub that records async_update_entry calls."""

    def __init__(self, entry: _FakeConfigEntry) -> None:
        self._entry = entry
        self.update_calls: list[tuple] = []

    def async_get_entry(self, entry_id: str):
        if self._entry.entry_id == entry_id:
            return self._entry
        return None

    def async_update_entry(self, entry, *, data):
        entry.data = dict(data)
        self.update_calls.append((entry, data))


class _FakeCadence:
    def __init__(self, interval_s: int = 300) -> None:
        self.interval_s = interval_s


class _FakeHass:
    def __init__(self, cadence=None, cadence_entry_id=None, keystore=None, entry=None) -> None:
        self.data: dict = {DOMAIN: {}}
        if keystore is not None:
            self.data[DOMAIN]["keystore"] = keystore
        if cadence is not None:
            self.data[DOMAIN]["cadence"] = cadence
        if cadence_entry_id is not None:
            self.data[DOMAIN]["cadence_entry_id"] = cadence_entry_id
        self.config_entries = (
            _FakeConfigEntries(entry)
            if entry is not None
            else _FakeConfigEntries(_FakeConfigEntry("__none__", {}))
        )


class _FakeRequest:
    """Minimal aiohttp-style request mock."""

    def __init__(
        self,
        hass_obj,
        *,
        island_key_header: str | None = None,
        authenticated: bool = False,
        body: dict | None = None,
    ) -> None:
        self.app = {"hass": hass_obj}
        self._data: dict = {"ha_authenticated": authenticated}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header
        self._body = body

    def get(self, key, default=None):  # noqa: D102
        return self._data.get(key, default)

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_setup(interval_s: int = 30):
    """Return (hass, cadence, entry, view) ready to test."""
    from custom_components.svitgrid.http_views import SvitgridCadenceView

    cadence = _FakeCadence(interval_s=interval_s)
    entry = _FakeConfigEntry("entry-1", {"harvest_interval_seconds": interval_s})
    keystore = _FakeKeystore(ISLAND_KEY)
    hass = _FakeHass(cadence=cadence, cadence_entry_id="entry-1", keystore=keystore, entry=entry)
    store = object()  # cadence view doesn't use store
    view = SvitgridCadenceView(store)
    return hass, cadence, entry, view


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_current_interval():
    """GET with valid island key returns current cadence."""
    hass, cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    resp = await view.get(request)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"intervalSeconds": 30}


@pytest.mark.asyncio
async def test_get_without_key_returns_401():
    """GET without island key → 401."""
    hass, _cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass)
    resp = await view.get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_wrong_key_returns_401():
    """GET with wrong island key → 401."""
    hass, _cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header="wrong-key")
    resp = await view.get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_missing_cadence_returns_404():
    """GET when cadence is not in hass.data → 404."""
    from custom_components.svitgrid.http_views import SvitgridCadenceView

    keystore = _FakeKeystore(ISLAND_KEY)
    hass = _FakeHass(keystore=keystore)  # no cadence set
    view = SvitgridCadenceView(object())
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    resp = await view.get(request)
    assert resp.status == 404


# ---------------------------------------------------------------------------
# PUT tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_valid_preset_updates_holder_and_entry():
    """PUT {intervalSeconds: 15} → 200, holder.interval_s == 15, entry persisted."""
    hass, cadence, entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": 15})
    resp = await view.put(request)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"intervalSeconds": 15}
    assert cadence.interval_s == 15
    # async_update_entry must have been called once with harvest_interval_seconds=15
    assert len(hass.config_entries.update_calls) == 1
    _entry_arg, data_arg = hass.config_entries.update_calls[0]
    assert data_arg["harvest_interval_seconds"] == 15


@pytest.mark.asyncio
async def test_put_invalid_preset_returns_400_and_holder_unchanged():
    """PUT {intervalSeconds: 7} (not a preset) → 400, holder unchanged."""
    hass, cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": 7})
    resp = await view.put(request)
    assert resp.status == 400
    assert cadence.interval_s == 30  # unchanged
    assert len(hass.config_entries.update_calls) == 0


@pytest.mark.asyncio
async def test_put_non_int_body_returns_400():
    """PUT {intervalSeconds: 'fast'} (non-int value) → 400, holder unchanged."""
    hass, cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": "fast"})
    resp = await view.put(request)
    assert resp.status == 400
    assert cadence.interval_s == 30


@pytest.mark.asyncio
async def test_put_missing_key_body_returns_400():
    """PUT {} (missing intervalSeconds key) → 400."""
    hass, cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={})
    resp = await view.put(request)
    assert resp.status == 400
    assert cadence.interval_s == 30


@pytest.mark.asyncio
async def test_put_without_key_returns_401():
    """PUT without island key → 401, holder unchanged."""
    hass, cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, body={"intervalSeconds": 15})
    resp = await view.put(request)
    assert resp.status == 401
    assert cadence.interval_s == 30


@pytest.mark.asyncio
async def test_put_missing_cadence_returns_404():
    """PUT when cadence is absent → 404."""
    from custom_components.svitgrid.http_views import SvitgridCadenceView

    keystore = _FakeKeystore(ISLAND_KEY)
    hass = _FakeHass(keystore=keystore)  # no cadence
    view = SvitgridCadenceView(object())
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": 60})
    resp = await view.put(request)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_put_all_valid_presets_accepted():
    """All five presets (5, 15, 30, 60, 300) are accepted."""
    from custom_components.svitgrid.http_views import SvitgridCadenceView

    for preset in (5, 15, 30, 60, 300):
        cadence = _FakeCadence(interval_s=300)
        entry = _FakeConfigEntry("e", {"harvest_interval_seconds": 300})
        keystore = _FakeKeystore(ISLAND_KEY)
        hass = _FakeHass(cadence=cadence, cadence_entry_id="e", keystore=keystore, entry=entry)
        view = SvitgridCadenceView(object())
        request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": preset})
        resp = await view.put(request)
        assert resp.status == 200, f"preset {preset} should be accepted"
        assert cadence.interval_s == preset
