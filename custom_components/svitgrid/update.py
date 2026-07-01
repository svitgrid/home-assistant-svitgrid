"""Home Assistant glue for auto-update: a DataUpdateCoordinator that checks
GitHub every 12h and auto-installs when a newer release exists, plus an
UpdateEntity shown in Settings → Updates."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_UPDATE,
    DOMAIN,
    GITHUB_REPO,
    RESTART_GUARD_WINDOW_S,
    UPDATE_CHECK_INTERVAL_S,
)
from .updater import ReleaseInfo, apply_update, fetch_latest_release, read_installed_version

_LOGGER = logging.getLogger(__name__)


class SvitgridUpdateCoordinator(DataUpdateCoordinator[ReleaseInfo | None]):
    """Polls GitHub for the latest release and drives the auto-install."""

    def __init__(
        self, hass, session, install_dir: Path, activity, get_auto_update: Callable[[], bool]
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="svitgrid_update",
            update_interval=timedelta(seconds=UPDATE_CHECK_INTERVAL_S),
        )
        self._session = session
        self._install_dir = install_dir
        self._activity = activity
        self._get_auto_update = get_auto_update
        self._installing = False
        try:
            self.installed_version = read_installed_version(install_dir)
        except Exception:  # noqa: BLE001
            self.installed_version = "0.0.0"

    async def _async_update_data(self) -> ReleaseInfo | None:
        release = await fetch_latest_release(self._session)
        if release is not None:
            await self._maybe_auto_install(release)
        return release

    async def _maybe_auto_install(self, release: ReleaseInfo) -> None:
        if release.version == self.installed_version:
            return
        if not self._get_auto_update():
            return
        if self._installing:
            return
        if self._is_restart_guarded():
            _LOGGER.info("Auto-update %s deferred: command ran recently", release.version)
            return
        await self.install(release)

    def _is_restart_guarded(self) -> bool:
        last = getattr(self._activity, "last_command_at", None)
        if last is None:
            return False
        return (dt_util.utcnow() - last).total_seconds() < RESTART_GUARD_WINDOW_S

    async def install(self, release: ReleaseInfo) -> None:
        """Download + swap files, then restart HA to load the new code.

        Single-flight: a concurrent auto- or manual-install is a no-op (the
        check-then-set is atomic on HA's single-threaded event loop)."""
        if self._installing:
            return
        self._installing = True
        try:
            _LOGGER.info("Installing svitgrid update %s", release.version)
            new_version = await apply_update(self._session, release.zip_url, self._install_dir)
            self.installed_version = new_version
            await self.hass.services.async_call("homeassistant", "restart")
        finally:
            self._installing = False


class SvitgridUpdateEntity(CoordinatorEntity[SvitgridUpdateCoordinator], UpdateEntity):
    _attr_has_entity_name = True
    _attr_name = "Svitgrid"
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_title = "Svitgrid"

    def __init__(
        self,
        coordinator: SvitgridUpdateCoordinator,
        entry_id: str,
        get_auto_update: Callable[[], bool],
    ):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_update"
        self._get_auto_update = get_auto_update

    @property
    def installed_version(self) -> str:
        return self.coordinator.installed_version

    @property
    def latest_version(self) -> str:
        release = self.coordinator.data
        return release.version if release else self.coordinator.installed_version

    @property
    def auto_update(self) -> bool:
        return self._get_auto_update()

    @property
    def in_progress(self) -> bool:
        return self.coordinator._installing

    @property
    def release_url(self) -> str:
        return f"https://github.com/{GITHUB_REPO}/releases"

    async def async_install(self, version, backup, **kwargs) -> None:
        release = self.coordinator.data
        if release is not None:
            await self.coordinator.install(release)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SvitgridUpdateCoordinator = data["update_coordinator"]

    def get_auto_update() -> bool:
        return entry.options.get(CONF_AUTO_UPDATE, True)

    async_add_entities([SvitgridUpdateEntity(coordinator, entry.entry_id, get_auto_update)])
