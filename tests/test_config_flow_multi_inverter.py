"""A finalized pairing may name several inverters.

`/finalize` used to describe ONE inverter in flat fields (`entityMap`, `brand`,
`model`, …) and the flow wrote exactly one entry into `inverters`. The API now
also returns an `inverters` array. When it is there it is authoritative — it is
the only thing that can describe a second unit — and the flat fields are read
only when it is absent, which is what an older API returns.

Getting the precedence backwards fails silently in the worst direction: the
entry looks healthy, the station shows one inverter, and the second one is
simply never polled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import DOMAIN


async def _run_pairing(hass: HomeAssistant, finalize_payload: dict) -> dict:
    """Walk the pair flow to completion and return the created entry's data."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        pass

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch(
            "custom_components.svitgrid.config_flow.asyncio.sleep",
            side_effect=_instant_sleep,
        ),
        patch(
            # The entry's SHAPE is what is under test; skip the real setup so
            # no background task fires.
            "custom_components.svitgrid.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "secret-multi", "code": "MULTI1", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(household_id="h-multi", preset_id="deye-a")
        )
        mock_client.finalize = AsyncMock(return_value=finalize_payload)

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    return dict(entries[0].data)


def _base_payload() -> dict:
    return {
        "edgeDeviceId": "ed-multi",
        "hardwareId": "ha-first",
        "apiKey": "k-multi",
        "householdId": "h-multi",
        "presetId": "deye-a",
        "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
        # Flat fields describe the FIRST inverter, exactly as they always have.
        "entityMap": {"batterySoc": "sensor.a_soc"},
        "brand": "Deye",
        "model": "SG04LP3",
        "phases": 3,
        "hasBattery": True,
        "pvStrings": 2,
        "commands": [],
    }


async def test_every_returned_inverter_reaches_the_entry(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    data = await _run_pairing(
        hass,
        {
            **_base_payload(),
            "inverters": [
                {
                    "inverterId": "ha-first",
                    "presetId": "deye-a",
                    "entityMap": {"batterySoc": "sensor.a_soc"},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "hasBattery": True,
                    "pvStrings": 2,
                    "commands": [],
                },
                {
                    "inverterId": "ha-second",
                    "presetId": "deye-b",
                    "entityMap": {"batterySoc": "sensor.b_soc"},
                    "brand": "Deye",
                    "model": "SG01LP1",
                    "phases": 1,
                    "hasBattery": False,
                    "pvStrings": 1,
                    "commands": [],
                },
            ],
        },
    )

    invs = data["inverters"]
    assert [i["inverter_id"] for i in invs] == ["ha-first", "ha-second"]
    # Each keeps its OWN mapping. One entity map applied to both is the failure
    # that would publish the first inverter's readings twice.
    assert invs[1]["entity_map"] == {"batterySoc": "sensor.b_soc"}
    assert invs[1]["model"] == "SG01LP1"
    assert invs[1]["has_battery"] is False


async def test_a_direct_modbus_inverter_keeps_its_own_address(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Two inverters polled over Modbus are two different addresses. Sharing
    one would poll the same unit twice and leave the other dark."""
    data = await _run_pairing(
        hass,
        {
            **_base_payload(),
            "inverters": [
                {
                    "inverterId": "ha-first",
                    "presetId": "deye-a",
                    "entityMap": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "hasBattery": True,
                    "pvStrings": 2,
                    "commands": [],
                    "harvestConfig": {
                        "protocol": "solarman_v5",
                        "ip": "192.168.1.10",
                        "port": 8899,
                        "slaveId": 1,
                        "modelId": "deye_sg04lp3",
                        "loggerSerial": "111",
                    },
                },
                {
                    "inverterId": "ha-second",
                    "presetId": "deye-a",
                    "entityMap": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "hasBattery": True,
                    "pvStrings": 2,
                    "commands": [],
                    "harvestConfig": {
                        "protocol": "solarman_v5",
                        "ip": "192.168.1.11",
                        "port": 8899,
                        "slaveId": 1,
                        "modelId": "deye_sg04lp3",
                        "loggerSerial": "222",
                    },
                },
            ],
        },
    )

    invs = data["inverters"]
    assert invs[0]["harvest_config"]["ip"] == "192.168.1.10"
    assert invs[1]["harvest_config"]["ip"] == "192.168.1.11"
    # Stored in the entry's own spelling. The setup loop reads `model_id`; a
    # camelCase copy stored verbatim leaves the spec unloaded and the inverter
    # polled by nothing (issue #5).
    assert invs[1]["harvest_config"]["logger_serial"] == "222"
    assert invs[0]["harvest_config"]["model_id"] == "deye_sg04lp3"
    assert invs[1]["harvest_config"]["slave_id"] == 1
    assert "loggerSerial" not in invs[1]["harvest_config"]


async def test_an_api_without_the_array_still_pairs_one_inverter(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The deployed API is not always ahead of the add-on. With no `inverters`
    key the flat fields are the only description there is, and the entry must
    come out exactly as it did before the array existed."""
    data = await _run_pairing(hass, _base_payload())

    invs = data["inverters"]
    assert len(invs) == 1
    assert invs[0]["inverter_id"] == "ha-first"
    assert invs[0]["entity_map"] == {"batterySoc": "sensor.a_soc"}
    assert invs[0]["model"] == "SG04LP3"


async def test_an_empty_array_is_not_read_as_a_station_with_no_inverter(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """An API that sends `inverters: []` alongside populated flat fields is
    describing one inverter it could not itemise — not a station with none.
    Reading the empty list literally leaves the publisher with nothing and the
    entry looking fine."""
    data = await _run_pairing(hass, {**_base_payload(), "inverters": []})

    assert len(data["inverters"]) == 1
    assert data["inverters"][0]["inverter_id"] == "ha-first"
