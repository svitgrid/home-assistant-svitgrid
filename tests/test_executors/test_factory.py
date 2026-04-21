"""Tests for the executor factory. SMG-II-specific behavior is in
test_smg_ii.py (next task)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.svitgrid.executors import create_executor
from custom_components.svitgrid.executors.base import BaseExecutor


def test_read_only_returns_none():
    hass = MagicMock()
    assert create_executor({"type": "read_only"}, hass) is None


def test_absent_type_returns_none():
    hass = MagicMock()
    assert create_executor({}, hass) is None


def test_unknown_type_raises():
    hass = MagicMock()
    with pytest.raises(ValueError, match="Unknown executor type"):
        create_executor({"type": "not_a_real_executor"}, hass)


def test_base_executor_is_abstract():
    with pytest.raises(TypeError):
        BaseExecutor()  # type: ignore[abstract]
