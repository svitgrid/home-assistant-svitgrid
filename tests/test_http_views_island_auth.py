"""TDD tests for island-key auth on read endpoints (Task 4 — island mode SP1).

Tests verify that each read view authorizes on HA-session OR X-Island-Key,
instead of HA-session-only.  Written BEFORE implementation (RED phase).
"""
from __future__ import annotations

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import (
    SvitgridHistoryView,
    SvitgridLiveView,
)

ISLAND_KEY = "test-island-key-abc123"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeKeystore:
    def __init__(self, island_key: str | None) -> None:
        self._island_key = island_key

    async def async_get_island_key(self) -> str | None:
        return self._island_key


class _FakeStore:
    async def live_snapshot(self):
        return [{"inverterId": "inv-1", "ts": "2026-06-24T10:00:00Z",
                 "payload": {"pvPower": 2.0}}]

    async def history_range(self, inverter_id, start, end):
        return [{"day": "2026-06-23", "sample_count": 5,
                 "avgs": {}, "peaks": {}, "energy": {}}]

    async def hourly_range(self, inverter_id, day):
        return []


class _FakeHeaders(dict):
    """Case-insensitive header dict matching aiohttp CIMultiDictProxy semantics."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeRequest:
    """Minimal aiohttp-style request mock that supports island-key auth."""

    def __init__(
        self,
        hass_obj,
        *,
        island_key_header: str | None = None,
        authenticated: bool = False,
        query: dict | None = None,
    ) -> None:
        self.app = {"hass": hass_obj}
        self.query = query or {}
        self._data: dict = {"ha_authenticated": authenticated}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header

    def get(self, key, default=None):  # noqa: D102
        return self._data.get(key, default)

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]


def _install_keystore(hass, island_key: str | None = ISLAND_KEY) -> None:
    """Wire a fake keystore into hass.data[DOMAIN]."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["keystore"] = _FakeKeystore(island_key)


# ---------------------------------------------------------------------------
# /api/svitgrid/live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_valid_island_key_no_session_returns_200(hass):
    """Valid X-Island-Key (no HA session) → 200 + JSON."""
    _install_keystore(hass)
    view = SvitgridLiveView(_FakeStore())
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    resp = await view.get(request)
    assert resp.status == 200
    assert b"inv-1" in resp.body


@pytest.mark.asyncio
async def test_live_no_key_no_session_returns_401(hass):
    """No island key + no HA session → 401."""
    _install_keystore(hass)
    view = SvitgridLiveView(_FakeStore())
    request = _FakeRequest(hass)
    resp = await view.get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_live_authenticated_session_no_key_returns_200(hass):
    """Authenticated HA session (no island key header) → 200 (regression: panel path)."""
    _install_keystore(hass)
    view = SvitgridLiveView(_FakeStore())
    request = _FakeRequest(hass, authenticated=True)
    resp = await view.get(request)
    assert resp.status == 200
    assert b"inv-1" in resp.body


@pytest.mark.asyncio
async def test_live_wrong_island_key_returns_401(hass):
    """Wrong X-Island-Key value → 401."""
    _install_keystore(hass)
    view = SvitgridLiveView(_FakeStore())
    request = _FakeRequest(hass, island_key_header="totally-wrong-key")
    resp = await view.get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_live_no_keystore_configured_session_authorized(hass):
    """No keystore in hass.data → island key path disabled; HA session still passes."""
    # Do NOT install keystore — simulates integration not yet set up.
    hass.data.setdefault(DOMAIN, {})
    view = SvitgridLiveView(_FakeStore())
    request = _FakeRequest(hass, authenticated=True)
    resp = await view.get(request)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_live_no_keystore_configured_no_session_returns_401(hass):
    """No keystore in hass.data + no session → 401 (key path disabled)."""
    hass.data.setdefault(DOMAIN, {})
    view = SvitgridLiveView(_FakeStore())
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    resp = await view.get(request)
    assert resp.status == 401


# ---------------------------------------------------------------------------
# /api/svitgrid/history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_valid_island_key_no_session_returns_200(hass):
    """Valid X-Island-Key on history → 200 + JSON."""
    _install_keystore(hass)
    view = SvitgridHistoryView(_FakeStore())
    request = _FakeRequest(
        hass,
        island_key_header=ISLAND_KEY,
        query={"start": "2026-06-20", "end": "2026-06-22"},
    )
    resp = await view.get(request)
    assert resp.status == 200
    assert b"days" in resp.body


@pytest.mark.asyncio
async def test_history_no_key_no_session_returns_401(hass):
    """No island key + no HA session → 401 on history."""
    _install_keystore(hass)
    view = SvitgridHistoryView(_FakeStore())
    request = _FakeRequest(hass, query={"start": "2026-06-20", "end": "2026-06-22"})
    resp = await view.get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_history_authenticated_session_no_key_returns_200(hass):
    """Authenticated HA session (no island key) → 200 on history (regression)."""
    _install_keystore(hass)
    view = SvitgridHistoryView(_FakeStore())
    request = _FakeRequest(hass, authenticated=True, query={})
    resp = await view.get(request)
    assert resp.status == 200
    assert b"days" in resp.body
