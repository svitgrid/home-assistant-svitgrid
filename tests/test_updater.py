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
