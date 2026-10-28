"""The pairing window lasts as long as the server keeps the code claimable.

`/start` answers `expiresIn`, the seconds the API accepts a claim for. The flow
used to wait a fixed 300 s while the API kept the code claimable for 1200 s, so
a claim made after minute five succeeded in the app — household created,
wizard reporting success — while the flow had already aborted with
`pairing_expired`, leaving a cloud station with no Home Assistant behind it
(svitgrid#1209).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import DOMAIN, PAIRING_MAX_POLL_DURATION_S
from custom_components.svitgrid.pairing_client import PairingPending


async def _start(hass: HomeAssistant, start_result: dict) -> tuple[dict, object]:
    with patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(return_value=start_result)
        mock_client.get_status = AsyncMock(return_value=PairingPending())
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    return result, hass.config_entries.flow._progress[result["flow_id"]]


def _stop(hass: HomeAssistant, flow, result: dict) -> None:
    flow._pair_task.cancel()
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_the_flow_waits_for_as_long_as_the_server_accepts_a_claim(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    result, flow = await _start(hass, {"secret": "s" * 40, "code": "7K9PA2", "expiresIn": 1200})
    assert result["type"] == "progress"

    remaining = flow._pair_deadline - hass.loop.time()
    assert 1190 < remaining <= 1200
    assert flow._local_session.expires_at == flow._pair_deadline

    _stop(hass, flow, result)


async def test_without_expires_in_the_flow_falls_back_to_the_default_window(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    result, flow = await _start(hass, {"secret": "s" * 40, "code": "7K9PA2"})

    remaining = flow._pair_deadline - hass.loop.time()
    assert PAIRING_MAX_POLL_DURATION_S - 10 < remaining <= PAIRING_MAX_POLL_DURATION_S

    _stop(hass, flow, result)
