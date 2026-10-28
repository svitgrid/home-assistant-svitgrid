"""The discovery snapshot's settings, handed to the add-on over the LAN.

The app reads an inverter's config registers while onboarding identifies it.
A cloud station gets them through the API. An island station with cloud sync
off keeps everything about its inverter in the house, so the app gives them to
the add-on instead, and `GET /api/svitgrid/settings` serves them straight
back: the owner lands on a filled settings page without waiting for the
settings loop's first read.

Settings only. A reading would go into `readings_raw`, which is queued for the
cloud and feeds the rollups, and the add-on's own first reading already
retries fast after setup.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import (
    SvitgridOnboardingSnapshotView,
    SvitgridSettingsView,
    register_views,
)
from custom_components.svitgrid.settings_sync import seed_onboarding_settings


class _MemoryStore:
    def __init__(self, fail: bool = False) -> None:
        self.saved: dict = {}
        self.fail = fail

    async def get_settings_snapshot(self, inverter_id):
        return self.saved.get(inverter_id)

    async def set_settings_snapshot(self, snapshot):
        if self.fail:
            raise OSError("disk full")
        self.saved[snapshot["inverter_id"]] = snapshot


def _inv(inverter_id: str = "inv-1", model_id: str = "deye_sg04lp3") -> dict:
    return {
        "inverter_id": inverter_id,
        "harvest_config": {
            "protocol": "solarman_v5",
            "ip": "192.168.1.50",
            "port": 8899,
            "slave_id": 1,
            "model_id": model_id,
            "logger_serial": "12345",
        },
    }


def _hass(inverters=None, snapshots=None, store=None):
    state = {
        "entry_data": {"inverters": [_inv()] if inverters is None else inverters},
        "settings_snapshots": {} if snapshots is None else snapshots,
        "store": store,
    }
    return SimpleNamespace(data={DOMAIN: {"entry-1": state}}), state


def _seed(hass, **overrides):
    args = {
        "model_id": "deye_sg04lp3",
        "start_register": 115,
        "registers": list(range(77)),
        "read_at": "2026-09-15T09:30:05.000Z",
    }
    args.update(overrides)
    return seed_onboarding_settings(hass, "inv-1", **args)


# ── The seeding rules ──────────────────────────────────────────────────


async def test_seeds_the_settings_the_view_serves_and_persists_them():
    store = _MemoryStore()
    hass, state = _hass(store=store)

    assert await _seed(hass) is None

    snapshot = state["settings_snapshots"]["inv-1"]
    assert snapshot == {
        "inverter_id": "inv-1",
        "model_id": "deye_sg04lp3",
        "start_register": 115,
        "registers": list(range(77)),
        # The settings loop's own format, so the view answers one shape.
        "read_at": "2026-09-15T09:30:05Z",
    }
    assert store.saved["inv-1"] == snapshot


@pytest.mark.parametrize(
    "inverters",
    [
        [],
        [_inv("inv-2")],
        # Relay inverters carry no register address space to mirror.
        [{"inverter_id": "inv-1", "entity_map": {}}],
    ],
)
async def test_an_inverter_this_add_on_does_not_harvest_is_refused(inverters):
    hass, state = _hass(inverters=inverters)

    assert await _seed(hass) == "unknown_inverter"
    assert state["settings_snapshots"] == {}


async def test_a_model_other_than_the_harvested_one_is_refused():
    # Its registers describe another address map.
    hass, state = _hass()

    assert (
        await _seed(hass, model_id="deye_sg03lp1", start_register=217, registers=list(range(63)))
        == "model_mismatch"
    )
    assert state["settings_snapshots"] == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_register": 217},
        {"registers": list(range(12))},
        {"registers": list(range(78))},
    ],
)
async def test_a_block_that_is_not_the_model_config_block_is_refused(overrides):
    # A partial or mis-aligned block maps to zeroed settings, which is how a
    # household's TOU schedule was wiped once.
    hass, state = _hass()

    assert await _seed(hass, **overrides) == "bad_range"
    assert state["settings_snapshots"] == {}


@pytest.mark.parametrize("bad", [-1, 65536, 1.5, "7", None, True])
async def test_a_register_that_is_not_a_16_bit_value_is_refused(bad):
    hass, state = _hass()
    registers = list(range(77))
    registers[40] = bad

    assert await _seed(hass, registers=registers) == "bad_registers"
    assert state["settings_snapshots"] == {}


@pytest.mark.parametrize("read_at", ["yesterday", "", None, 1757928605])
async def test_an_unreadable_read_time_is_refused(read_at):
    hass, state = _hass()

    assert await _seed(hass, read_at=read_at) == "bad_read_at"
    assert state["settings_snapshots"] == {}


async def test_the_add_on_own_read_is_never_replaced():
    # The settings loop reads after setup, so its read is fresher than one the
    # phone made during discovery.
    own = {
        "inverter_id": "inv-1",
        "model_id": "deye_sg04lp3",
        "start_register": 115,
        "registers": [9] * 77,
        "read_at": "2026-09-15T09:31:00Z",
    }
    hass, state = _hass(snapshots={"inv-1": own})

    assert await _seed(hass) == "already_read"
    assert state["settings_snapshots"]["inv-1"] is own


async def test_a_store_that_cannot_persist_still_serves_the_settings():
    hass, state = _hass(store=_MemoryStore(fail=True))

    assert await _seed(hass) is None
    assert state["settings_snapshots"]["inv-1"]["registers"] == list(range(77))


# ── POST /api/svitgrid/onboarding-snapshot ─────────────────────────────


class _Req:
    def __init__(self, hass, query=None, body=None, authenticated=True) -> None:
        self.app = {"hass": hass}
        self.query = query or {}
        self.headers = {}
        self._body = body
        self._data = {"ha_authenticated": authenticated}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


_BODY = {
    "settings": {
        "modelId": "deye_sg04lp3",
        "startRegister": 115,
        "registers": list(range(77)),
        "readAt": "2026-09-15T09:30:05.000Z",
    },
}


async def _post(hass, *, query=None, body=None, authenticated=True):
    view = SvitgridOnboardingSnapshotView(store=None)
    return await view.post(
        _Req(
            hass,
            {"inverter_id": "inv-1"} if query is None else query,
            _BODY if body is None else body,
            authenticated,
        )
    )


async def test_view_requires_auth():
    hass, state = _hass()

    resp = await _post(hass, authenticated=False)

    assert resp.status == 401
    assert state["settings_snapshots"] == {}


async def test_view_without_inverter_id_is_a_bad_request():
    hass, _ = _hass()

    resp = await _post(hass, query={})

    assert resp.status == 400
    assert json.loads(resp.body) == {"error": "missing_inverter_id"}


@pytest.mark.parametrize(
    "body",
    [ValueError("not json"), [], {"settings": "x"}, {"reading": {"pvPower": 1}}],
)
async def test_view_without_a_settings_object_is_a_bad_request(body):
    hass, _ = _hass()

    resp = await _post(hass, body=body)

    assert resp.status == 400
    assert json.loads(resp.body) == {"error": "invalid_body"}


async def test_view_for_an_inverter_it_does_not_harvest_is_not_found():
    hass, _ = _hass(inverters=[_inv("inv-2")])

    resp = await _post(hass)

    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "unknown_inverter"}


async def test_view_seeds_the_settings_and_the_settings_view_serves_them():
    hass, _ = _hass()

    resp = await _post(hass)

    # The API route's own answer shape, so the app reads both with one parser.
    assert resp.status == 200
    assert json.loads(resp.body) == {"settings": {"ok": True}, "reading": None}

    served = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-1"}))
    assert served.status == 200
    assert json.loads(served.body)["registers"] == list(range(77))


async def test_view_reports_a_refused_block_without_failing_the_request():
    hass, _ = _hass()
    body = {"settings": {**_BODY["settings"], "registers": list(range(12))}}

    resp = await _post(hass, body=body)

    assert resp.status == 200
    assert json.loads(resp.body) == {
        "settings": {"ok": False, "error": "bad_range"},
        "reading": None,
    }


async def test_view_ignores_a_reading():
    # A store with no append at all: storing the reading would raise.
    hass, _ = _hass(store=_MemoryStore())
    body = {**_BODY, "reading": {"inverterId": "inv-1", "pvPower": 1200}}

    resp = await _post(hass, body=body)

    assert resp.status == 200
    assert json.loads(resp.body)["reading"] is None


# ── Brand bodies other than the Deye block ─────────────────────────────


def _lux_inv() -> dict:
    inv = _inv(model_id="luxpower_sna_5000_wpv")
    inv["harvest_config"]["protocol"] = "modbus_tcp"
    return inv


def _lux_settings(**overrides) -> dict:
    settings = {
        "modelId": "luxpower_sna_5000_wpv",
        "protocol": "luxpower",
        "familyCode": "cBaa",
        "blocks": [
            {"startRegister": start, "registers": list(range(40))}
            for start in (0, 40, 80, 120, 160)
        ],
        "readAt": "2026-09-15T09:30:05.000Z",
    }
    settings.update(overrides)
    return settings


async def test_onboarding_snapshot_accepts_luxpower_body():
    hass, state = _hass(inverters=[_lux_inv()])

    resp = await _post(hass, body={"settings": _lux_settings()})

    assert json.loads(resp.body)["settings"] == {"ok": True}
    served = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-1"}))
    assert served.status == 200
    out = json.loads(served.body)
    assert out["protocol"] == "luxpower"
    assert out["familyCode"] == "cBaa"
    assert [b["startRegister"] for b in out["blocks"]] == [0, 40, 80, 120, 160]
    assert out["readAt"] == "2026-09-15T09:30:05Z"


async def test_onboarding_snapshot_refuses_unknown_body_with_bad_body():
    hass, state = _hass(inverters=[_lux_inv()])
    # A Deye-shaped body for a LuxPower inverter is no LuxPower body.
    body = {"settings": {**_BODY["settings"], "modelId": "luxpower_sna_5000_wpv"}}

    resp = await _post(hass, body=body)

    assert json.loads(resp.body)["settings"] == {"ok": False, "error": "bad_body"}
    assert state["settings_snapshots"] == {}


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"blocks": [{"startRegister": 0, "registers": list(range(200))}]}, "bad_range"),
        (
            {
                "blocks": [
                    {"startRegister": s, "registers": [70000] * 40} for s in (0, 40, 80, 120, 160)
                ]
            },
            "bad_registers",
        ),
        ({"familyCode": 7}, "bad_body"),
    ],
)
async def test_onboarding_snapshot_validates_the_luxpower_blocks(overrides, error):
    hass, state = _hass(inverters=[_lux_inv()])

    resp = await _post(hass, body={"settings": _lux_settings(**overrides)})

    assert json.loads(resp.body)["settings"] == {"ok": False, "error": error}
    assert state["settings_snapshots"] == {}


async def test_onboarding_snapshot_accepts_a_must_body():
    inv = _inv(model_id="must_pv18_5248")
    inv["harvest_config"]["protocol"] = "modbus_tcp"
    hass, state = _hass(inverters=[inv])
    settings = {
        "modelId": "must_pv18_5248",
        "protocol": "must",
        "edition": 10415,
        "packVoltage": 48,
        "blocks": [
            {"startRegister": 10101, "registers": list(range(12))},
            {"startRegister": 20101, "registers": list(range(43))},
        ],
        "readAt": "2026-09-15T09:30:05.000Z",
    }

    resp = await _post(hass, body={"settings": settings})

    assert json.loads(resp.body)["settings"] == {"ok": True}
    body = state["settings_snapshots"]["inv-1"]["body"]
    # The integer edition is stored as the firmware's text, as a read makes it.
    assert body["edition"] == "1.04.15"
    assert body["packVoltage"] == 48


async def test_onboarding_snapshot_accepts_a_solis_body_and_refuses_a_missing_address():
    from custom_components.svitgrid.settings_read_plan import SOLIS_SETTINGS_ADDRESSES

    inv = _inv(model_id="solis_s6_eh3p_h")
    inv["harvest_config"].update(protocol="modbus_tcp", port=502)
    registers = {str(a): 1 for a in SOLIS_SETTINGS_ADDRESSES}
    base = {"modelId": "solis_s6_eh3p_h", "protocol": "solis", "readAt": "2026-09-15T09:30:05Z"}

    hass, state = _hass(inverters=[inv])
    short = dict(registers)
    short.pop(str(SOLIS_SETTINGS_ADDRESSES[0]))
    refused = await _post(hass, body={"settings": {**base, "registers": short}})
    assert json.loads(refused.body)["settings"] == {"ok": False, "error": "bad_range"}

    accepted = await _post(hass, body={"settings": {**base, "registers": registers}})
    assert json.loads(accepted.body)["settings"] == {"ok": True}
    assert state["settings_snapshots"]["inv-1"]["kind"] == "solis"


def test_view_is_registered():
    registered = []
    hass = SimpleNamespace(http=SimpleNamespace(register_view=registered.append))
    register_views(hass, store=None)
    assert any(isinstance(v, SvitgridOnboardingSnapshotView) for v in registered)
    assert SvitgridOnboardingSnapshotView.url == "/api/svitgrid/onboarding-snapshot"
