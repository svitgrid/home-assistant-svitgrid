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


async def fetch_release_zip(session: Any, zip_url: str) -> bytes:
    """Download the release archive. Raises UpdateValidationError on non-200."""
    async with session.get(zip_url, headers=_HEADERS) as resp:
        if resp.status != 200:
            raise UpdateValidationError(f"download failed: status={resp.status}")
        return await resp.read()


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract, guarding against zip-slip (entries escaping dest)."""
    dest_root = dest.resolve()
    for name in zf.namelist():
        target = (dest / name).resolve()
        if target != dest_root and dest_root not in target.parents:
            raise UpdateValidationError(f"unsafe archive entry: {name}")
    zf.extractall(dest)


def apply_update_bytes(raw: bytes, install_dir: Path, work_dir: Path) -> str:
    """Validate the archive in `raw` and atomically swap it into install_dir.

    `work_dir` MUST be outside HA's custom_components/ — it holds the staging
    tree and the retained backup, so no manifest-bearing scratch dir is left
    where HA scans for integrations. Same-filesystem os.replace makes the swap
    atomic. Pure/sync — the caller offloads it to an executor. Raises
    UpdateValidationError (live dir untouched) on an invalid archive; restores
    from backup on a later failure. Returns the newly-installed version."""
    work_dir.mkdir(parents=True, exist_ok=True)
    staging = work_dir / "svitgrid.new"
    incoming = work_dir / "svitgrid.incoming"
    backup = work_dir / "svitgrid.bak"
    for tmp in (staging, incoming):
        if tmp.exists():
            shutil.rmtree(tmp)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            _safe_extract(zf, staging)
        source = _find_package_dir(staging)  # raises UpdateValidationError if absent
        new_version = read_installed_version(source)

        shutil.copytree(source, incoming)
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(install_dir, backup)  # atomic: live -> backup
        try:
            os.replace(incoming, install_dir)  # atomic: new -> live
        except Exception:
            os.replace(backup, install_dir)  # restore live
            raise
        return new_version
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(incoming, ignore_errors=True)
