"""Fix 2 — a cadence-only PUT must NOT reload the config entry.

The harvest loop reads cadence.interval_s live every tick, so persisting a new
cadence needs no reload. The PUT handler sets a `_cadence_only_update` flag in
hass.data[DOMAIN] right before async_update_entry; the update listener
(_async_reload_entry) consumes that flag and skips the reload.

Written BEFORE implementation (RED phase).
"""

from __future__ import annotations

import json

import pytest

from custom_components.svitgrid.const import DOMAIN

# ---------------------------------------------------------------------------
# Fakes (mirror test_http_views_cadence.py)
# ---------------------------------------------------------------------------


class _FakeKeystore:
    def __init__(self, island_key):
        self._island_key = island_key

    async def async_get_island_key(self):
        return self._island_key

    async def async_get_island_keys(self):
        return [self._island_key] if self._island_key else []


class _FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)


class _FakeConfigEntry:
    def __init__(self, entry_id, data):
        self.entry_id = entry_id
        self.data = dict(data)


class _FakeConfigEntries:
    def __init__(self, entry):
        self._entry = entry
        self.update_calls = []

    def async_get_entry(self, entry_id):
        return self._entry if self._entry.entry_id == entry_id else None

    def async_update_entry(self, entry, *, data):
        entry.data = dict(data)
        self.update_calls.append((entry, data))


class _FakeCadence:
    def __init__(self, interval_s=300):
        self.interval_s = interval_s


class _FakeHass:
    def __init__(self, cadence, cadence_entry_id, keystore, entry):
        self.data = {DOMAIN: {}}
        self.data[DOMAIN]["keystore"] = keystore
        self.data[DOMAIN]["cadence"] = cadence
        self.data[DOMAIN]["cadence_entry_id"] = cadence_entry_id
        self.config_entries = _FakeConfigEntries(entry)


class _FakeRequest:
    def __init__(self, hass_obj, *, island_key_header=None, body=None):
        self.app = {"hass": hass_obj}
        self._data = {"ha_authenticated": False}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header
        self._body = body

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


ISLAND_KEY = "test-island-key-no-reload"


def _make_setup(interval_s=30):
    from custom_components.svitgrid.http_views import SvitgridCadenceView

    cadence = _FakeCadence(interval_s=interval_s)
    entry = _FakeConfigEntry("entry-1", {"harvest_interval_seconds": interval_s})
    hass = _FakeHass(cadence, "entry-1", _FakeKeystore(ISLAND_KEY), entry)
    return hass, cadence, entry, SvitgridCadenceView(object())


# ---------------------------------------------------------------------------
# PUT sets the cadence-only flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_sets_cadence_only_update_flag_before_update():
    """A valid PUT flags the update as cadence-only so the reload is skipped."""
    hass, _cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": 15})
    resp = await view.put(request)
    assert resp.status == 200
    assert json.loads(resp.body) == {"intervalSeconds": 15}
    # async_update_entry ran (persistence) AND the flag was set.
    assert len(hass.config_entries.update_calls) == 1
    assert hass.data[DOMAIN].get("_cadence_only_update") is True


@pytest.mark.asyncio
async def test_put_invalid_does_not_set_flag():
    """A rejected PUT must not set the cadence-only flag (no update happened)."""
    hass, _cadence, _entry, view = _make_setup(interval_s=30)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body={"intervalSeconds": 7})
    resp = await view.put(request)
    assert resp.status == 400
    assert hass.data[DOMAIN].get("_cadence_only_update") is None


# ---------------------------------------------------------------------------
# _async_reload_entry consumes the flag
# ---------------------------------------------------------------------------


class _ReloadHass:
    def __init__(self, flag_value):
        self.data = {DOMAIN: {}}
        if flag_value is not None:
            self.data[DOMAIN]["_cadence_only_update"] = flag_value
        self.config_entries = _ReloadConfigEntries()


class _ReloadConfigEntries:
    def __init__(self):
        self.reload_calls = []

    async def async_reload(self, entry_id):
        self.reload_calls.append(entry_id)


class _ReloadEntry:
    entry_id = "entry-xyz"


@pytest.mark.asyncio
async def test_reload_entry_skips_when_cadence_only_flag_set():
    """_async_reload_entry returns without reloading when the flag is set, and
    clears the flag so a subsequent real update still reloads."""
    from custom_components.svitgrid import _async_reload_entry

    hass = _ReloadHass(flag_value=True)
    await _async_reload_entry(hass, _ReloadEntry())
    assert hass.config_entries.reload_calls == []
    # Flag consumed (popped).
    assert "_cadence_only_update" not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_reload_entry_reloads_when_flag_absent():
    """_async_reload_entry reloads normally for any non-cadence update."""
    from custom_components.svitgrid import _async_reload_entry

    hass = _ReloadHass(flag_value=None)
    await _async_reload_entry(hass, _ReloadEntry())
    assert hass.config_entries.reload_calls == ["entry-xyz"]


@pytest.mark.asyncio
async def test_reload_entry_skips_when_skip_reload_once_flag_set():
    """_async_reload_entry skips its reload when `_skip_reload_once` is set (the
    caller — harvest_config_apply — does its own explicit reload), preventing a
    double reload / two overlapping setups. The flag is consumed."""
    from custom_components.svitgrid import _async_reload_entry

    hass = _ReloadHass(flag_value=None)
    hass.data[DOMAIN]["_skip_reload_once"] = True
    await _async_reload_entry(hass, _ReloadEntry())
    assert hass.config_entries.reload_calls == []
    assert "_skip_reload_once" not in hass.data[DOMAIN]
