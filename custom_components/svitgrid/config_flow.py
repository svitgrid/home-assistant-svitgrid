"""Svitgrid config flow.

Phase 1: Pair branch only. Manual branch raises NotImplementedError —
ships in Phase 4.
"""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class SvitgridConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Svitgrid setup flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First step — present Pair vs Manual."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["pair", "manual"],
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manual entity-mapping branch — Phase 4."""
        return self.async_abort(reason="manual_branch_not_implemented")

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pair branch — Task 13b implements this."""
        return self.async_abort(reason="not_implemented_yet")
