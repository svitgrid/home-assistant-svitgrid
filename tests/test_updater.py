"""Unit tests for the pure auto-update core (no Home Assistant imports)."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.updater import (
    ReleaseInfo,
    UpdateValidationError,
    apply_update_bytes,
    fetch_latest_release,
    fetch_release_zip,
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
    session = _mock_get(
        200,
        json_body={
            "tag_name": "v0.11.0",
            "zipball_url": "https://api.github.com/repos/svitgrid/home-assistant-svitgrid/zipball/v0.11.0",
        },
    )
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
async def test_fetch_release_zip_returns_bytes():
    session = _mock_get(200, read_body=b"zip-bytes-here")
    raw = await fetch_release_zip(session, "http://zip")
    assert raw == b"zip-bytes-here"


@pytest.mark.asyncio
async def test_fetch_release_zip_non_200_raises():
    session = _mock_get(404, read_body=b"")
    with pytest.raises(UpdateValidationError):
        await fetch_release_zip(session, "http://zip")


def test_apply_update_bytes_swaps_files_and_backs_up(tmp_path: Path):
    _write_integration(tmp_path, "0.10.1")
    install_dir = tmp_path / "custom_components" / "svitgrid"
    work_dir = tmp_path / "work"
    raw = _zipball_bytes("0.11.0")

    new_version = apply_update_bytes(raw, install_dir, work_dir)

    assert new_version == "0.11.0"
    assert json.loads((install_dir / "manifest.json").read_text())["version"] == "0.11.0"
    assert (install_dir / "sentinel.py").read_text() == "# 0.11.0\n"
    # A backup of the previous version is retained in work_dir, OUTSIDE
    # custom_components/ so HA's integration loader never sees it.
    backup = work_dir / "svitgrid.bak"
    assert json.loads((backup / "manifest.json").read_text())["version"] == "0.10.1"


def test_apply_update_bytes_invalid_zip_leaves_live_dir_untouched(tmp_path: Path):
    _write_integration(tmp_path, "0.10.1")
    install_dir = tmp_path / "custom_components" / "svitgrid"
    work_dir = tmp_path / "work"
    # A zip with no custom_components/svitgrid/manifest.json inside.
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("junk/readme.txt", "nope")

    with pytest.raises(UpdateValidationError):
        apply_update_bytes(bad.getvalue(), install_dir, work_dir)

    assert json.loads((install_dir / "manifest.json").read_text())["version"] == "0.10.1"
    assert (install_dir / "sentinel.py").read_text() == "# 0.10.1\n"


def test_apply_update_bytes_rejects_zip_slip(tmp_path: Path):
    _write_integration(tmp_path, "0.10.1")
    install_dir = tmp_path / "custom_components" / "svitgrid"
    work_dir = tmp_path / "work"

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        base = "svitgrid-home-assistant-svitgrid-abc123/custom_components/svitgrid"
        zf.writestr(
            f"{base}/manifest.json", json.dumps({"domain": "svitgrid", "version": "0.11.0"})
        )
        zf.writestr(f"{base}/sentinel.py", "# 0.11.0\n")
        zf.writestr("../evil.py", "x")

    with pytest.raises(UpdateValidationError):
        apply_update_bytes(evil.getvalue(), install_dir, work_dir)

    assert json.loads((install_dir / "manifest.json").read_text())["version"] == "0.10.1"
    assert (install_dir / "sentinel.py").read_text() == "# 0.10.1\n"


def test_apply_update_bytes_restores_backup_on_swap_failure(tmp_path, monkeypatch):
    _write_integration(tmp_path, "0.10.1")
    install_dir = tmp_path / "custom_components" / "svitgrid"
    work_dir = tmp_path / "work"
    raw = _zipball_bytes("0.11.0")

    import custom_components.svitgrid.updater as updater_mod

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the incoming -> install_dir swap
            raise OSError("boom")
        return real_replace(src, dst)

    monkeypatch.setattr(updater_mod.os, "replace", flaky_replace)

    with pytest.raises(OSError):
        apply_update_bytes(raw, install_dir, work_dir)

    # The live dir must be restored to the original version, not left missing.
    assert json.loads((install_dir / "manifest.json").read_text())["version"] == "0.10.1"
    assert (install_dir / "sentinel.py").read_text() == "# 0.10.1\n"
