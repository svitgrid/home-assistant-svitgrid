"""Pure auto-update mechanics: query GitHub, download a release zip, and
atomically swap the integration's own files. No Home Assistant imports so it
can be unit-tested with temp dirs and fake zips."""

from __future__ import annotations

import io
import json
import logging
import os
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

        # Stage the validated package as a sibling of install_dir so the swap
        # uses same-filesystem atomic renames (no multi-file copy window during
        # which install_dir would be half-populated or missing).
        incoming = install_dir.parent / "svitgrid.incoming"
        if incoming.exists():
            shutil.rmtree(incoming)
        shutil.copytree(source, incoming)

        # Back up the current install, then swap via two atomic renames.
        # If the second rename fails, restore the backup so install_dir is
        # never left missing.
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(install_dir, backup)        # atomic: live -> backup
        try:
            os.replace(incoming, install_dir)  # atomic: new -> live
        except Exception:
            os.replace(backup, install_dir)    # restore live
            raise
        return new_version
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(install_dir.parent / "svitgrid.incoming", ignore_errors=True)
