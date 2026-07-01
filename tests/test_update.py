"""Tests for the update coordinator's auto-install decision logic."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.svitgrid.update import _is_newer
from custom_components.svitgrid.updater import ReleaseInfo


def _coordinator(hass, *, installed="0.10.1", auto_update=True, last_command_at=None):
    from custom_components.svitgrid.update import SvitgridUpdateCoordinator

    activity = SimpleNamespace(last_command_at=last_command_at)
    coord = SvitgridUpdateCoordinator(
        hass,
        session=AsyncMock(),
        install_dir=Path("/tmp/does-not-matter"),
        activity=activity,
        get_auto_update=lambda: auto_update,
    )
    coord.installed_version = installed
    return coord


@pytest.mark.asyncio
async def test_auto_installs_when_newer_and_idle(hass):
    coord = _coordinator(hass, installed="0.10.1", auto_update=True, last_command_at=None)
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with (
        patch(
            "custom_components.svitgrid.update.fetch_latest_release",
            AsyncMock(return_value=release),
        ),
        patch(
            "custom_components.svitgrid.update.fetch_release_zip",
            AsyncMock(return_value=b"zip"),
        ) as m_fetch,
        patch(
            "custom_components.svitgrid.update.apply_update_bytes",
            MagicMock(return_value="0.11.0"),
        ) as m_apply,
        patch.object(type(hass.services), "async_call", AsyncMock()) as m_restart,
    ):
        await coord._async_update_data()
    m_fetch.assert_awaited_once()
    m_apply.assert_called_once()
    m_restart.assert_awaited_once_with("homeassistant", "restart")
    assert coord.installed_version == "0.11.0"


@pytest.mark.asyncio
async def test_no_install_when_auto_update_off(hass):
    coord = _coordinator(hass, installed="0.10.1", auto_update=False)
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with (
        patch(
            "custom_components.svitgrid.update.fetch_latest_release",
            AsyncMock(return_value=release),
        ),
        patch("custom_components.svitgrid.update.apply_update_bytes", MagicMock()) as m_apply,
        patch.object(type(hass.services), "async_call", AsyncMock()) as m_restart,
    ):
        data = await coord._async_update_data()
    m_apply.assert_not_called()
    m_restart.assert_not_awaited()
    assert data == release  # entity still advertises the newer version


@pytest.mark.asyncio
async def test_defers_when_command_ran_recently(hass):
    coord = _coordinator(
        hass,
        installed="0.10.1",
        auto_update=True,
        last_command_at=dt_util.utcnow() - timedelta(seconds=5),
    )
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with (
        patch(
            "custom_components.svitgrid.update.fetch_latest_release",
            AsyncMock(return_value=release),
        ),
        patch("custom_components.svitgrid.update.apply_update_bytes", MagicMock()) as m_apply,
        patch.object(type(hass.services), "async_call", AsyncMock()) as m_restart,
    ):
        await coord._async_update_data()
    m_apply.assert_not_called()
    m_restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_install_when_already_latest(hass):
    coord = _coordinator(hass, installed="0.11.0", auto_update=True)
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with (
        patch(
            "custom_components.svitgrid.update.fetch_latest_release",
            AsyncMock(return_value=release),
        ),
        patch("custom_components.svitgrid.update.apply_update_bytes", MagicMock()) as m_apply,
    ):
        await coord._async_update_data()
    m_apply.assert_not_called()


@pytest.mark.asyncio
async def test_install_is_single_flight(hass):
    coord = _coordinator(hass, installed="0.10.1", auto_update=True)
    coord._installing = True  # simulate an install already running
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with (
        patch("custom_components.svitgrid.update.apply_update_bytes", MagicMock()) as m_apply,
        patch.object(type(hass.services), "async_call", AsyncMock()) as m_restart,
    ):
        await coord.install(release)
    m_apply.assert_not_called()
    m_restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_install_on_downgrade(hass):
    coord = _coordinator(hass, installed="0.11.0", auto_update=True)
    release = ReleaseInfo("v0.10.0", "0.10.0", "http://zip")
    with (
        patch(
            "custom_components.svitgrid.update.fetch_latest_release",
            AsyncMock(return_value=release),
        ),
        patch("custom_components.svitgrid.update.apply_update_bytes", MagicMock()) as m_apply,
        patch.object(type(hass.services), "async_call", AsyncMock()) as m_restart,
    ):
        await coord._async_update_data()
    m_apply.assert_not_called()
    m_restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_restart_when_installed_not_newer(hass):
    coord = _coordinator(hass, installed="0.10.1", auto_update=True)
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with (
        patch(
            "custom_components.svitgrid.update.fetch_release_zip",
            AsyncMock(return_value=b"zip"),
        ),
        patch(
            "custom_components.svitgrid.update.apply_update_bytes",
            MagicMock(return_value="0.10.1"),
        ) as m_apply,
        patch.object(type(hass.services), "async_call", AsyncMock()) as m_restart,
    ):
        await coord.install(release)
    m_apply.assert_called_once()
    m_restart.assert_not_awaited()


def test_is_newer():
    assert _is_newer("0.11.0", "0.10.1") is True
    assert _is_newer("0.10.1", "0.10.1") is False
    assert _is_newer("0.10.0", "0.11.0") is False
    assert _is_newer("garbage", "0.1") is False
