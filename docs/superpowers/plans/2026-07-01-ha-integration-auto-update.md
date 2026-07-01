# HA Integration Auto-Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Svitgrid HACS integration keep itself on the latest GitHub release automatically, via a native Home Assistant `UpdateEntity` with a guarded auto-restart and a per-install opt-out.

**Architecture:** A pure, HA-free `updater.py` does the risky work (query GitHub `releases/latest`, download the release zip, validate + atomically swap the integration's own files with one backup). A thin `update.py` wraps it in a `DataUpdateCoordinator` (checks every 12h; auto-installs when a newer version exists, auto-update is enabled, and no command ran in the last 60s) plus an `UpdateEntity` shown in *Settings → Updates*. A config-flow option toggles auto-update (default on). The command poller reports the running version as a header so the cloud can see stragglers.

**Tech Stack:** Python 3.11+, Home Assistant custom integration, `aiohttp`, `pytest` + `pytest-homeassistant-custom-component`.

## Global Constraints

- Repo: `home-assistant-svitgrid`; integration package: `custom_components/svitgrid/`; current version `0.10.1`.
- GitHub source of truth: `svitgrid/home-assistant-svitgrid`, endpoint `https://api.github.com/repos/svitgrid/home-assistant-svitgrid/releases/latest`. All GitHub HTTP calls MUST send a `User-Agent` header (GitHub rejects requests without one).
- Always-latest: no cloud version endpoint, no min-required floor, no staged rollout.
- Fail-open: any network/parse/rate-limit failure logs and skips the tick; the live install directory is never left partially written.
- Auto-restart is required (code loads only on restart) and MUST be deferred while a command executed within `RESTART_GUARD_WINDOW_S` (60s).
- Auto-update option defaults **on**.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Run tests with `pytest` from the repo root.

---

### Task 1: `updater.py` — pure update core

**Files:**
- Create: `custom_components/svitgrid/updater.py`
- Modify: `custom_components/svitgrid/const.py` (append one constant)
- Test: `tests/test_updater.py`

**Interfaces:**
- Consumes: nothing (HA-free; takes an `aiohttp.ClientSession` and a `pathlib.Path`).
- Produces:
  - `ReleaseInfo` dataclass: `tag: str`, `version: str`, `zip_url: str`.
  - `class UpdateValidationError(Exception)`.
  - `def read_installed_version(install_dir: Path) -> str` — reads `manifest.json`'s `version`.
  - `async def fetch_latest_release(session) -> ReleaseInfo | None`.
  - `async def apply_update(session, zip_url: str, install_dir: Path) -> str` — returns the newly-installed version.

- [ ] **Step 1: Add the GitHub repo constant**

Append to `custom_components/svitgrid/const.py`:

```python
# ── auto-update ────────────────────────────────────────────────────────
GITHUB_REPO = "svitgrid/home-assistant-svitgrid"
GITHUB_LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
GITHUB_USER_AGENT = "svitgrid-ha-integration"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_updater.py`:

```python
"""Unit tests for the pure auto-update core (no Home Assistant imports)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.updater import (
    ReleaseInfo,
    UpdateValidationError,
    apply_update,
    fetch_latest_release,
    read_installed_version,
)


def _mock_get(status: int, *, json_body=None, read_body: bytes = b""):
    """Mock aiohttp session whose .get(...) async-context yields a response."""
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body)
    resp.read = AsyncMock(return_value=read_body)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    session.get = MagicMock(return_value=resp)
    return session


def _write_integration(dir_: Path, version: str) -> None:
    pkg = dir_ / "custom_components" / "svitgrid"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text(json.dumps({"domain": "svitgrid", "version": version}))
    (pkg / "sentinel.py").write_text(f"# {version}\n")


def _zipball_bytes(version: str, top_dir: str = "svitgrid-home-assistant-svitgrid-abc123") -> bytes:
    """Build a GitHub-style zipball: a single top-level wrapper dir containing the repo."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        base = f"{top_dir}/custom_components/svitgrid"
        zf.writestr(f"{base}/manifest.json", json.dumps({"domain": "svitgrid", "version": version}))
        zf.writestr(f"{base}/sentinel.py", f"# {version}\n")
    return buf.getvalue()


def test_read_installed_version(tmp_path: Path):
    _write_integration(tmp_path, "0.10.1")
    assert read_installed_version(tmp_path / "custom_components" / "svitgrid") == "0.10.1"


@pytest.mark.asyncio
async def test_fetch_latest_release_parses_tag_and_zipball():
    session = _mock_get(200, json_body={
        "tag_name": "v0.11.0",
        "zipball_url": "https://api.github.com/repos/svitgrid/home-assistant-svitgrid/zipball/v0.11.0",
    })
    info = await fetch_latest_release(session)
    assert info == ReleaseInfo(
        tag="v0.11.0",
        version="0.11.0",
        zip_url="https://api.github.com/repos/svitgrid/home-assistant-svitgrid/zipball/v0.11.0",
    )


@pytest.mark.asyncio
async def test_fetch_latest_release_non_200_returns_none():
    session = _mock_get(403, json_body={})
    assert await fetch_latest_release(session) is None


@pytest.mark.asyncio
async def test_apply_update_swaps_files_and_backs_up(tmp_path: Path):
    _write_integration(tmp_path, "0.10.1")
    install_dir = tmp_path / "custom_components" / "svitgrid"
    session = _mock_get(200, read_body=_zipball_bytes("0.11.0"))

    new_version = await apply_update(session, "http://zip", install_dir)

    assert new_version == "0.11.0"
    assert json.loads((install_dir / "manifest.json").read_text())["version"] == "0.11.0"
    assert (install_dir / "sentinel.py").read_text() == "# 0.11.0\n"
    # A backup of the previous version is retained alongside install_dir.
    backup = install_dir.parent / "svitgrid.bak"
    assert json.loads((backup / "manifest.json").read_text())["version"] == "0.10.1"


@pytest.mark.asyncio
async def test_apply_update_invalid_zip_leaves_live_dir_untouched(tmp_path: Path):
    _write_integration(tmp_path, "0.10.1")
    install_dir = tmp_path / "custom_components" / "svitgrid"
    # A zip with no custom_components/svitgrid/manifest.json inside.
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("junk/readme.txt", "nope")
    session = _mock_get(200, read_body=bad.getvalue())

    with pytest.raises(UpdateValidationError):
        await apply_update(session, "http://zip", install_dir)

    assert json.loads((install_dir / "manifest.json").read_text())["version"] == "0.10.1"
    assert (install_dir / "sentinel.py").read_text() == "# 0.10.1\n"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_updater.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.svitgrid.updater'`.

- [ ] **Step 4: Implement `updater.py`**

Create `custom_components/svitgrid/updater.py`:

```python
"""Pure auto-update mechanics: query GitHub, download a release zip, and
atomically swap the integration's own files. No Home Assistant imports so it
can be unit-tested with temp dirs and fake zips."""

from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .const import GITHUB_LATEST_RELEASE_URL, GITHUB_USER_AGENT

_LOGGER = logging.getLogger(__name__)

_HEADERS = {"User-Agent": GITHUB_USER_AGENT, "Accept": "application/vnd.github+json"}


class UpdateValidationError(Exception):
    """The downloaded archive did not contain a valid svitgrid integration."""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str
    zip_url: str


def read_installed_version(install_dir: Path) -> str:
    """Read the `version` field from the manifest.json in install_dir."""
    manifest = json.loads((install_dir / "manifest.json").read_text())
    return str(manifest["version"])


async def fetch_latest_release(session: Any) -> ReleaseInfo | None:
    """GET the latest GitHub release. Returns None on any non-200/parse error
    (fail-open — the caller simply retries on the next tick)."""
    try:
        async with session.get(GITHUB_LATEST_RELEASE_URL, headers=_HEADERS) as resp:
            if resp.status != 200:
                _LOGGER.debug("fetch_latest_release: status=%s", resp.status)
                return None
            data = await resp.json()
        tag = str(data["tag_name"])
        zip_url = str(data["zipball_url"])
    except Exception:  # noqa: BLE001
        _LOGGER.debug("fetch_latest_release failed", exc_info=True)
        return None
    return ReleaseInfo(tag=tag, version=tag.lstrip("v"), zip_url=zip_url)


def _find_package_dir(extracted_root: Path) -> Path:
    """Locate the `custom_components/svitgrid` dir inside an extracted archive.
    GitHub zipballs wrap everything in a single top-level dir, so we search."""
    for manifest in extracted_root.rglob("custom_components/svitgrid/manifest.json"):
        return manifest.parent
    raise UpdateValidationError("archive has no custom_components/svitgrid/manifest.json")


async def apply_update(session: Any, zip_url: str, install_dir: Path) -> str:
    """Download `zip_url`, validate it, back up the current install_dir to a
    sibling `svitgrid.bak`, and atomically swap in the new files. Returns the
    newly-installed version. Raises UpdateValidationError (live dir untouched)
    if the archive is invalid; restores from backup on a later failure."""
    async with session.get(zip_url, headers=_HEADERS) as resp:
        if resp.status != 200:
            raise UpdateValidationError(f"download failed: status={resp.status}")
        raw = await resp.read()

    staging = install_dir.parent / "svitgrid.new"
    backup = install_dir.parent / "svitgrid.bak"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(staging)
        source = _find_package_dir(staging)  # raises UpdateValidationError if absent
        new_version = read_installed_version(source)

        # Point of no return: back up then swap. Restore on failure.
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(install_dir, backup)
        try:
            shutil.rmtree(install_dir)
            shutil.copytree(source, install_dir)
        except Exception:
            shutil.rmtree(install_dir, ignore_errors=True)
            shutil.copytree(backup, install_dir)
            raise
        return new_version
    finally:
        shutil.rmtree(staging, ignore_errors=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_updater.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add custom_components/svitgrid/updater.py custom_components/svitgrid/const.py tests/test_updater.py
git commit -m "feat(update): pure GitHub release fetch + atomic file swap core"
```

---

### Task 2: `update.py` — coordinator + entity

**Files:**
- Create: `custom_components/svitgrid/update.py`
- Modify: `custom_components/svitgrid/const.py` (append constants)
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes from Task 1: `ReleaseInfo`, `apply_update`, `fetch_latest_release`, `read_installed_version`.
- Produces:
  - `class SvitgridUpdateCoordinator(DataUpdateCoordinator)` with attrs `installed_version: str`, `data: ReleaseInfo | None`, methods `async def install(release: ReleaseInfo) -> None`, `_is_restart_guarded() -> bool`, and boolean `_installing`. Constructor: `(hass, session, install_dir: Path, activity, get_auto_update: Callable[[], bool])`.
  - `class SvitgridUpdateEntity(CoordinatorEntity, UpdateEntity)`.
  - `async def async_setup_entry(hass, entry, async_add_entities)` (the `update` platform).

- [ ] **Step 1: Add coordinator/entity constants**

Append to `custom_components/svitgrid/const.py`:

```python
UPDATE_CHECK_INTERVAL_S = 12 * 3600  # how often to poll GitHub for a new release
RESTART_GUARD_WINDOW_S = 60          # defer auto-restart if a command ran this recently
CONF_AUTO_UPDATE = "auto_update"     # entry-options key; default True
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_update.py`:

```python
"""Tests for the update coordinator's auto-install decision logic."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util

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
    with patch("custom_components.svitgrid.update.fetch_latest_release", AsyncMock(return_value=release)), \
         patch("custom_components.svitgrid.update.apply_update", AsyncMock(return_value="0.11.0")) as m_apply, \
         patch.object(hass.services, "async_call", AsyncMock()) as m_restart:
        await coord._async_update_data()
    m_apply.assert_awaited_once()
    m_restart.assert_awaited_once_with("homeassistant", "restart")
    assert coord.installed_version == "0.11.0"


@pytest.mark.asyncio
async def test_no_install_when_auto_update_off(hass):
    coord = _coordinator(hass, installed="0.10.1", auto_update=False)
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with patch("custom_components.svitgrid.update.fetch_latest_release", AsyncMock(return_value=release)), \
         patch("custom_components.svitgrid.update.apply_update", AsyncMock()) as m_apply, \
         patch.object(hass.services, "async_call", AsyncMock()) as m_restart:
        data = await coord._async_update_data()
    m_apply.assert_not_awaited()
    m_restart.assert_not_awaited()
    assert data == release  # entity still advertises the newer version


@pytest.mark.asyncio
async def test_defers_when_command_ran_recently(hass):
    coord = _coordinator(
        hass, installed="0.10.1", auto_update=True,
        last_command_at=dt_util.utcnow() - timedelta(seconds=5),
    )
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with patch("custom_components.svitgrid.update.fetch_latest_release", AsyncMock(return_value=release)), \
         patch("custom_components.svitgrid.update.apply_update", AsyncMock()) as m_apply, \
         patch.object(hass.services, "async_call", AsyncMock()) as m_restart:
        await coord._async_update_data()
    m_apply.assert_not_awaited()
    m_restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_install_when_already_latest(hass):
    coord = _coordinator(hass, installed="0.11.0", auto_update=True)
    release = ReleaseInfo("v0.11.0", "0.11.0", "http://zip")
    with patch("custom_components.svitgrid.update.fetch_latest_release", AsyncMock(return_value=release)), \
         patch("custom_components.svitgrid.update.apply_update", AsyncMock()) as m_apply:
        await coord._async_update_data()
    m_apply.assert_not_awaited()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_update.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.svitgrid.update'`.

- [ ] **Step 4: Implement `update.py`**

Create `custom_components/svitgrid/update.py`:

```python
"""Home Assistant glue for auto-update: a DataUpdateCoordinator that checks
GitHub every 12h and auto-installs when a newer release exists, plus an
UpdateEntity shown in Settings → Updates."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Callable

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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

    def __init__(self, hass, session, install_dir: Path, activity, get_auto_update: Callable[[], bool]):
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
        """Download + swap files, then restart HA to load the new code."""
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

    def __init__(self, coordinator: SvitgridUpdateCoordinator, entry_id: str, get_auto_update: Callable[[], bool]):
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
    get_auto = lambda: entry.options.get(CONF_AUTO_UPDATE, True)
    async_add_entities([SvitgridUpdateEntity(coordinator, entry.entry_id, get_auto)])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_update.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add custom_components/svitgrid/update.py custom_components/svitgrid/const.py tests/test_update.py
git commit -m "feat(update): coordinator + UpdateEntity with guarded auto-install"
```

---

### Task 3: Wire the update platform into setup

**Files:**
- Modify: `custom_components/svitgrid/__init__.py`
- Test: `tests/test_update_setup.py`

**Interfaces:**
- Consumes from Task 2: `SvitgridUpdateCoordinator`, and the `update` platform's `async_setup_entry`.
- Produces: `hass.data[DOMAIN][entry.entry_id]["update_coordinator"]` populated; `"update"` added to the forwarded platforms.

- [ ] **Step 1: Write the failing test**

Create `tests/test_update_setup.py`:

```python
"""Verify the update coordinator is created and the platform is forwarded."""

from __future__ import annotations

import pytest


def test_update_platform_is_forwarded():
    # The list passed to async_forward_entry_setups must include "update".
    import custom_components.svitgrid as init_mod

    src = (init_mod.__file__)
    text = open(src).read()
    assert '"update"' in text and "async_forward_entry_setups" in text
    # And the coordinator must be stored for the platform to read.
    assert '"update_coordinator"' in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_update_setup.py -v`
Expected: FAIL — assertion error (`"update"` / `"update_coordinator"` not yet present).

- [ ] **Step 3: Create the coordinator and forward the platform**

In `custom_components/svitgrid/__init__.py`, add imports near the other local imports at the top of the file:

```python
from pathlib import Path

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_AUTO_UPDATE
from .update import SvitgridUpdateCoordinator
```

In `async_setup_entry`, immediately before the existing `hass.data.setdefault(DOMAIN, {})` block, create the coordinator (note: `activity` is already defined earlier in this function):

```python
    update_coordinator = SvitgridUpdateCoordinator(
        hass,
        session=async_get_clientsession(hass),
        install_dir=Path(__file__).parent,
        activity=activity,
        get_auto_update=lambda: entry.options.get(CONF_AUTO_UPDATE, True),
    )
```

Add it to the stored `hass.data[DOMAIN][entry.entry_id]` dict (append one key inside that literal):

```python
        "update_coordinator": update_coordinator,
```

Change the forward call from:

```python
    await hass.config_entries.async_forward_entry_setups(
        entry, ["sensor", "binary_sensor"]
    )
```

to:

```python
    await hass.config_entries.async_forward_entry_setups(
        entry, ["sensor", "binary_sensor", "update"]
    )
```

Immediately after that forward call, kick off the first check in the background (so a pending update installs shortly after startup without blocking setup):

```python
    hass.async_create_background_task(
        update_coordinator.async_refresh(), name="svitgrid_update_first_check"
    )
```

In `async_unload_entry`, change:

```python
    await hass.config_entries.async_unload_platforms(entry, ["sensor", "binary_sensor"])
```

to:

```python
    await hass.config_entries.async_unload_platforms(
        entry, ["sensor", "binary_sensor", "update"]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_update_setup.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `pytest tests/test_update.py tests/test_updater.py tests/test_update_setup.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/svitgrid/__init__.py tests/test_update_setup.py
git commit -m "feat(update): create coordinator and forward the update platform"
```

---

### Task 4: Auto-update opt-out in the options flow

**Files:**
- Modify: `custom_components/svitgrid/config_flow.py`
- Test: `tests/test_options_auto_update.py`

**Interfaces:**
- Consumes from Task 2: `CONF_AUTO_UPDATE` (already in `const.py`).
- Produces: a `settings` step in `SvitgridOptionsFlow` that writes `{CONF_AUTO_UPDATE: bool}` into entry options via `async_create_entry`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_options_auto_update.py`:

```python
"""The options flow exposes an auto-update toggle that persists to options."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import CONF_AUTO_UPDATE, DOMAIN


@pytest.mark.asyncio
async def test_settings_step_persists_auto_update(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"inverters": []}, options={})
    entry.add_to_hass(hass)

    from custom_components.svitgrid.config_flow import SvitgridOptionsFlow

    flow = SvitgridOptionsFlow(entry)
    flow.hass = hass

    # Show the form.
    result = await flow.async_step_settings()
    assert result["type"] == "form"
    assert result["step_id"] == "settings"

    # Submit auto-update = False.
    result = await flow.async_step_settings({CONF_AUTO_UPDATE: False})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_AUTO_UPDATE] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_options_auto_update.py -v`
Expected: FAIL — `AttributeError: 'SvitgridOptionsFlow' object has no attribute 'async_step_settings'`.

- [ ] **Step 3: Implement the settings step**

In `custom_components/svitgrid/config_flow.py`, add `CONF_AUTO_UPDATE` to the `const` import, then add `"settings"` to the menu in `async_step_init`:

```python
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_inverter", "edit_inverter", "remove_inverter", "settings"],
        )
```

Add the step method to `SvitgridOptionsFlow` (voluptuous `vol` and `BooleanSelector` are already imported in this file):

```python
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="", data={CONF_AUTO_UPDATE: bool(user_input[CONF_AUTO_UPDATE])}
            )
        current = self._entry.options.get(CONF_AUTO_UPDATE, True)
        schema = vol.Schema({
            vol.Required(CONF_AUTO_UPDATE, default=current): BooleanSelector(),
        })
        return self.async_show_form(step_id="settings", data_schema=schema)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_options_auto_update.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/config_flow.py tests/test_options_auto_update.py
git commit -m "feat(update): auto-update opt-out in options flow (default on)"
```

---

### Task 5: Report the running version to the cloud (straggler telemetry)

**Files:**
- Modify: `custom_components/svitgrid/api_client.py:144-163` (`poll_commands`)
- Modify: `custom_components/svitgrid/command_poller.py` (`run_loop` signature + `poll_commands` call)
- Modify: `custom_components/svitgrid/__init__.py` (pass the running version into the command loop)
- Test: `tests/test_poll_version_header.py`

**Interfaces:**
- Consumes from Task 1: `read_installed_version`.
- Produces: `poll_commands(api_key, integration_version=None)` sends header `x-integration-version`; `run_loop(..., integration_version: str | None = None)` forwards it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_poll_version_header.py`:

```python
"""poll_commands sends the running integration version as a header."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import SvitgridApiClient


def _mock_session(status=200, json_body=None):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {"commands": []})
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    session.get = MagicMock(return_value=resp)
    return session


@pytest.mark.asyncio
async def test_poll_commands_sends_integration_version_header():
    session = _mock_session()
    client = SvitgridApiClient(session, api_base="https://api.example")
    await client.poll_commands(api_key="k" * 10, integration_version="0.11.0")
    _, kwargs = session.get.call_args
    assert kwargs["headers"]["x-integration-version"] == "0.11.0"
    assert kwargs["headers"]["x-api-key"] == "k" * 10


@pytest.mark.asyncio
async def test_poll_commands_omits_header_when_version_none():
    session = _mock_session()
    client = SvitgridApiClient(session, api_base="https://api.example")
    await client.poll_commands(api_key="k" * 10)
    _, kwargs = session.get.call_args
    assert "x-integration-version" not in kwargs["headers"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_poll_version_header.py -v`
Expected: FAIL — `TypeError: poll_commands() got an unexpected keyword argument 'integration_version'`.

- [ ] **Step 3: Add the header in `poll_commands`**

In `custom_components/svitgrid/api_client.py`, change the `poll_commands` signature and header construction:

```python
    async def poll_commands(
        self, api_key: str, integration_version: str | None = None
    ) -> dict[str, Any]:
        url = f"{self._base}/api/v3/executors/commands"
        headers = {"x-api-key": api_key}
        if integration_version:
            headers["x-integration-version"] = integration_version
        async with self._session.get(url, headers=headers) as resp:
```

(Leave the rest of the method body unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_poll_version_header.py -v`
Expected: PASS.

- [ ] **Step 5: Thread the version through the poller**

In `custom_components/svitgrid/command_poller.py`, add `integration_version: str | None = None` to the `run_loop` keyword-only params (next to `executor_version`), and update the `poll_commands` call at line ~503:

```python
            resp = await api_client.poll_commands(
                api_key=state.api_key, integration_version=integration_version
            )
```

- [ ] **Step 6: Pass the running version from setup**

In `custom_components/svitgrid/__init__.py`, at the `run_command_loop(...)` call, add the argument (reuse the `Path(__file__).parent` install dir; import `read_installed_version` from `.updater`):

```python
                integration_version=read_installed_version(Path(__file__).parent),
```

Add near the other imports:

```python
from .updater import read_installed_version
```

- [ ] **Step 7: Run the full update-related suite**

Run: `pytest tests/test_poll_version_header.py tests/test_update.py tests/test_updater.py tests/test_update_setup.py tests/test_options_auto_update.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add custom_components/svitgrid/api_client.py custom_components/svitgrid/command_poller.py custom_components/svitgrid/__init__.py tests/test_poll_version_header.py
git commit -m "feat(update): report running integration version on command poll"
```

---

### Task 6: Docs + full regression run

**Files:**
- Modify: `RELEASE-NOTES.md` (if present in this repo) or `CHANGELOG.md`
- Modify: `README.md` (short "Automatic updates" section)

- [ ] **Step 1: Add a changelog entry**

Under the `[Unreleased]` (or top) section of `CHANGELOG.md`, add:

```markdown
- Automatic updates: the integration now keeps itself on the latest GitHub release
  and restarts Home Assistant to apply it. Toggle it off under
  Settings → Devices & Services → Svitgrid → Configure → Settings.
```

- [ ] **Step 2: Add a README note**

Add a short section to `README.md`:

```markdown
## Automatic updates

Svitgrid checks GitHub for a new release every 12 hours and installs it
automatically (Home Assistant restarts to apply the new code). It never restarts
while a command is running. Turn it off in
**Settings → Devices & Services → Svitgrid → Configure → Settings**; you can then
update manually from **Settings → Updates**.
```

- [ ] **Step 3: Run the entire test suite**

Run: `pytest -q`
Expected: all PASS (report, but do not fix, any pre-existing unrelated failures).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs(update): document automatic updates"
```

---

## Self-Review

**Spec coverage:**
- UpdateEntity mechanism, self-contained → Tasks 2, 3. ✓
- Guarded auto-restart (never mid-command) → Task 2 (`_is_restart_guarded`, `RESTART_GUARD_WINDOW_S`). ✓
- GitHub release zip download + atomic swap + one backup → Task 1 (`apply_update`, `svitgrid.bak`). ✓
- Always-latest, no cloud endpoint/floor/rollout → nothing built for these (correct). ✓
- Config opt-out, default on → Task 4. ✓
- `integrationVersion` telemetry → Task 5 (implemented as the `x-integration-version` poll header — refinement over the spec's "poll payload"; covers idle installs, which the ACK-based `executorVersion` would miss). ✓
- Fail-open error handling → Task 1 (`fetch_latest_release` returns None; `apply_update` restores/aborts). ✓
- Tests for each behavior → each task is TDD. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step is complete. ✓

**Type consistency:** `ReleaseInfo(tag, version, zip_url)` used identically in Tasks 1, 2, 3, 5. `read_installed_version(install_dir)` used in Tasks 1, 2, 5. `SvitgridUpdateCoordinator(hass, session, install_dir, activity, get_auto_update)` matches its construction in Task 3. `CONF_AUTO_UPDATE` used in Tasks 2, 3, 4. `poll_commands(api_key, integration_version=None)` matches its call in Task 5. ✓

**Note on the optional monorepo follow-up:** recording `x-integration-version` server-side in `services/api` (so the straggler view is queryable) is out of scope here, per the design. This plan only ensures the header is sent.
