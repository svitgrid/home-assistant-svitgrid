"""Answer the owner's question about a cloud claim in a config flow test.

A cloud claim of the pairing code finalizes nothing until the owner approves
it in Home Assistant (svitgrid#1200), so a test that pairs through the cloud
taps Approve the way the owner would.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import DOMAIN


async def approve_cloud_claims(hass: HomeAssistant) -> list[dict]:
    """Approve every cloud claim a svitgrid flow is asking about. Returns the
    flow results the approvals produced."""
    await hass.async_block_till_done()
    results = []
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        if flow.get("step_id") == "pair_cloud_approval":
            results.append(
                await hass.config_entries.flow.async_configure(
                    flow["flow_id"], {"next_step_id": "pair_cloud_approve"}
                )
            )
    await hass.async_block_till_done()
    return results
