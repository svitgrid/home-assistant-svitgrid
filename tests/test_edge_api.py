"""The edge-facing views (edge island, Phase 1, Task 2, svitgrid#1203).

An edge box pointed at the integration calls the subset of the cloud edge API
it always calls, at the same paths, with its `X-API-Key`. These views answer
it: `/health` and `/api/v3/me`, and a fixed answer for every path only the
cloud can serve. Two rules run through all of them: never 410 (a 410 evicts
the box), and nothing is reachable while no edge box is adopted (404
`no_edge_boxes`, before any auth check), so an install that never adopts a
box sees no change.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from multidict import CIMultiDict

from custom_components.svitgrid import edge_api, settings_sync
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import register_views
from custom_components.svitgrid.keystore import EdgeBoxEntry, SvitgridKeystore

BOX_KEY = "box-api-key-1"
ENTRY_ID = "entry-abc"


class _FakeStore:
    def __init__(self, data):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


def _box(device_id="EC-1", api_key=BOX_KEY, inverters=None) -> EdgeBoxEntry:
    inverters = inverters if inverters is not None else {"inv-1": {"ipAddress": "10.0.0.5"}}
    return EdgeBoxEntry(
        device_id=device_id,
        api_key=api_key,
        inverter_ids=list(inverters),
        inverters=inverters,
        added_at="2026-10-28T10:00:00+00:00",
        pair_code_hash=None,
        pair_code_expires_at=None,
    )


def _keystore(*boxes: EdgeBoxEntry, trusted: dict | None = None) -> SvitgridKeystore:
    ks = SvitgridKeystore.__new__(SvitgridKeystore)
    ks._trust_listeners = []
    ks._store = _FakeStore(
        {
            "api_key": "ak",
            "public_key_hex": "04ff",
            "private_key_pem": "pem",
            "signing_key_id": "ha-1",
            "trusted_key_ids": sorted(trusted or {}),
            "trusted_public_keys_hex": dict(trusted or {}),
            "edge_boxes": {
                b.device_id: {
                    "device_id": b.device_id,
                    "api_key": b.api_key,
                    "inverter_ids": b.inverter_ids,
                    "inverters": b.inverters,
                    "added_at": b.added_at,
                    "pair_code_hash": b.pair_code_hash,
                    "pair_code_expires_at": b.pair_code_expires_at,
                }
                for b in boxes
            },
        }
    )
    return ks


def _hass(keystore) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"keystore": keystore}}
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    hass.config_entries.async_entries.return_value = [entry]
    return hass


class _FakeRequest:
    def __init__(self, hass, *, api_key: str | None = None, body=None, match_info=None):
        self.app = {"hass": hass}
        self.headers = CIMultiDict()
        if api_key is not None:
            self.headers["X-API-Key"] = api_key
        self.match_info = match_info or {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def _json(resp) -> dict:
    import json

    return json.loads(resp.body)


# Every reading and grid event here carries a fixed 2026-10-28 timestamp, and
# the pipeline refuses one more than 24 hours old, so the views' clock is pinned
# near them (#1215: these tests went red 24 hours after they were written).
_NOW = datetime(2026, 10, 28, 10, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(edge_api, "_now", lambda: _NOW)


async def _call(view_cls, method, hass, **kwargs):
    view = view_cls()
    request = _FakeRequest(hass, **kwargs)
    handler = getattr(view, method)
    return await handler(request, **request.match_info)


# (view class, method, match_info) for every view this task adds.
EDGE_VIEWS = [
    (edge_api.EdgeHealthView, "get", {}),
    (edge_api.EdgeMeView, "get", {}),
    (edge_api.EdgeBootstrapView, "post", {}),
    (edge_api.EdgeFirmwareCheckView, "get", {}),
    (edge_api.EdgeMqttTokenView, "post", {"device_id": "EC-1"}),
    (edge_api.EdgeCoredumpView, "post", {}),
    (edge_api.EdgePairAttemptsView, "post", {"device_id": "EC-1"}),
    (edge_api.EdgeUpdateInverterIpView, "post", {}),
    (edge_api.EdgeZigbeeSinkView, "post", {}),
    (edge_api.EdgeZigbeeSinkView, "post", {"device_id": "EC-1"}),
    (edge_api.EdgeChargerSinkView, "post", {}),
    (edge_api.EdgeChargerSinkView, "post", {"command_id": "cmd-1"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), EDGE_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_every_edge_view_is_404_without_adopted_boxes(view_cls, method, match_info):
    """An install that never adopted a box answers as if the paths did not
    exist, before any auth check: a valid-looking key changes nothing."""
    hass = _hass(_keystore())
    for api_key in (None, BOX_KEY):
        resp = await _call(view_cls, method, hass, api_key=api_key, match_info=match_info)
        assert resp.status == 404
        assert _json(resp) == {"error": "no_edge_boxes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), EDGE_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_every_edge_view_is_404_with_no_keystore(view_cls, method, match_info):
    """Before setup stores a keystore, the gate still answers 404."""
    hass = _hass(None)
    resp = await _call(view_cls, method, hass, api_key=BOX_KEY, match_info=match_info)
    assert resp.status == 404
    assert _json(resp) == {"error": "no_edge_boxes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"),
    [v for v in EDGE_VIEWS if v[0] not in (edge_api.EdgeHealthView, edge_api.EdgeBootstrapView)],
    ids=lambda v: getattr(v, "__name__", None),
)
async def test_missing_api_key_is_401_not_410(view_cls, method, match_info):
    hass = _hass(_keystore(_box()))
    for api_key in (None, "", "not-a-bound-key", "not-a-key-with-a-code"):
        resp = await _call(view_cls, method, hass, api_key=api_key, match_info=match_info)
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}


# ── Task 11: the pairing code binds the box's key on first contact ──────


async def _me(hass, *, api_key=None, pair_code=None, view_cls=None, method="get", **kwargs):
    view = (view_cls or edge_api.EdgeMeView)()
    request = _FakeRequest(hass, api_key=api_key, **kwargs)
    if pair_code is not None:
        request.headers["X-Pair-Code"] = pair_code
    return await getattr(view, method)(request, **request.match_info)


async def _bound_keys(keystore) -> dict:
    state = await keystore.load()
    return {d: b.api_key for d, b in state.edge_boxes.items()}


@pytest.mark.asyncio
async def test_matching_code_binds_key_and_is_consumed():
    ks = _keystore(_box(api_key=None), _box(device_id="EC-2", api_key=None))
    code = await ks.async_issue_pair_code("EC-2")
    hass = _hass(ks)

    resp = await _me(hass, api_key="new-key-2", pair_code=code)
    assert resp.status == 200
    assert _json(resp)["deviceId"] == "EC-2"
    assert await _bound_keys(ks) == {"EC-1": None, "EC-2": "new-key-2"}
    state = await ks.load()
    assert state.edge_boxes["EC-2"].pair_code_hash is None
    assert state.edge_boxes["EC-2"].pair_code_expires_at is None

    # Bound: the key alone now works.
    resp = await _me(hass, api_key="new-key-2")
    assert resp.status == 200
    assert _json(resp)["deviceId"] == "EC-2"

    # Single use: the same code with another key binds nothing.
    resp = await _me(hass, api_key="other-key", pair_code=code)
    assert resp.status == 403
    assert _json(resp) == {"error": "pair_code_invalid"}
    assert await _bound_keys(ks) == {"EC-1": None, "EC-2": "new-key-2"}


@pytest.mark.asyncio
async def test_code_of_another_box_is_refused():
    """Two boxes switching in the same ten minutes (review focus 1)."""
    ks = _keystore(_box(api_key=None), _box(device_id="EC-2", api_key=None))
    code_1 = await ks.async_issue_pair_code("EC-1")
    await ks.async_issue_pair_code("EC-2")
    hass = _hass(ks)

    # A box that names itself EC-2 in the path presents EC-1's code.
    for view_cls, method in (
        (edge_api.EdgeMqttTokenView, "post"),
        (edge_api.EdgePairAttemptsView, "post"),
    ):
        resp = await _me(
            hass,
            api_key="key-of-ec-2",
            pair_code=code_1,
            view_cls=view_cls,
            method=method,
            match_info={"device_id": "EC-2"},
        )
        assert resp.status == 403
        assert _json(resp) == {"error": "pair_code_invalid"}
    assert await _bound_keys(ks) == {"EC-1": None, "EC-2": None}

    # A code issued to a box that is already bound is refused too.
    resp = await _me(hass, api_key="key-of-ec-1", pair_code=code_1)
    assert resp.status == 200
    resp = await _me(hass, api_key="key-of-ec-2", pair_code=code_1)
    assert resp.status == 403
    assert _json(resp) == {"error": "pair_code_invalid"}
    assert await _bound_keys(ks) == {"EC-1": "key-of-ec-1", "EC-2": None}


@pytest.mark.asyncio
async def test_bound_key_cannot_be_rebound_by_a_code():
    ks = _keystore(_box(), _box(device_id="EC-2", api_key=None))
    code_2 = await ks.async_issue_pair_code("EC-2")
    hass = _hass(ks)

    resp = await _me(hass, api_key=BOX_KEY, pair_code=code_2)
    assert resp.status == 403
    assert _json(resp) == {"error": "pair_code_invalid"}
    assert await _bound_keys(ks) == {"EC-1": BOX_KEY, "EC-2": None}
    # The refused code is not consumed: EC-2 still binds with it.
    state = await ks.load()
    assert state.edge_boxes["EC-2"].pair_code_hash is not None
    resp = await _me(hass, api_key="key-of-ec-2", pair_code=code_2)
    assert resp.status == 200
    assert _json(resp)["deviceId"] == "EC-2"


@pytest.mark.asyncio
async def test_expired_code_answers_pair_code_expired(monkeypatch):
    from custom_components.svitgrid import keystore as keystore_mod

    ks = _keystore(_box(api_key=None))
    code = await ks.async_issue_pair_code("EC-1")
    later = datetime.now(UTC) + keystore_mod.EDGE_PAIR_CODE_TTL
    monkeypatch.setattr(keystore_mod, "_utcnow", lambda: later)
    hass = _hass(ks)

    resp = await _me(hass, api_key="new-key", pair_code=code)
    assert resp.status == 403
    assert _json(resp) == {"error": "pair_code_expired"}
    assert await _bound_keys(ks) == {"EC-1": None}


@pytest.mark.asyncio
async def test_unknown_key_without_code_is_401():
    ks = _keystore(_box(api_key=None))
    await ks.async_issue_pair_code("EC-1")
    hass = _hass(ks)
    for api_key in (None, "", "unbound"):
        resp = await _me(hass, api_key=api_key)
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}
    # A code with no key has nothing to bind.
    code = await ks.async_issue_pair_code("EC-1")
    for api_key in (None, ""):
        resp = await _me(hass, api_key=api_key, pair_code=code)
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}
    assert await _bound_keys(ks) == {"EC-1": None}


@pytest.mark.asyncio
async def test_unknown_code_answers_pair_code_invalid_never_410():
    hass = _hass(_keystore(_box()))
    resp = await _me(hass, api_key="unbound", pair_code="ABCDEF")
    assert resp.status == 403
    assert _json(resp) == {"error": "pair_code_invalid"}


@pytest.mark.asyncio
async def test_bound_box_with_stale_header_is_accepted():
    """A bound box that still sends its spent code after a reboot."""
    ks = _keystore(_box(api_key=None))
    code = await ks.async_issue_pair_code("EC-1")
    hass = _hass(ks)
    assert (await _me(hass, api_key="new-key", pair_code=code)).status == 200

    resp = await _me(hass, api_key="new-key", pair_code=code)
    assert resp.status == 200
    assert _json(resp)["deviceId"] == "EC-1"
    resp = await _me(hass, api_key="new-key", pair_code="ZZZZZZ")
    assert resp.status == 200
    assert await _bound_keys(ks) == {"EC-1": "new-key"}


@pytest.mark.asyncio
async def test_code_on_ingest_binds_too():
    """A reboot between the `/me` probe and the switch: the first contact is
    an ingest batch, and it binds and is stored."""
    ks = _keystore(_box(api_key=None, inverters={"inv-1": _INV_1}))
    code = await ks.async_issue_pair_code("EC-1")
    store = _FakeIngestStore()
    hass = _hass_with_store(ks, store)
    reading = _reading("inv-1", "2026-10-28T10:00:00.000Z")

    resp = await _me(
        hass,
        api_key="new-key",
        pair_code=code,
        view_cls=edge_api.EdgeIngestBatchView,
        method="post",
        body={"readings": [reading]},
    )
    assert resp.status == 200
    assert _json(resp)["results"] == [{"ok": True, "inverterId": "inv-1"}]
    assert len(store.appended) == 1
    assert await _bound_keys(ks) == {"EC-1": "new-key"}


@pytest.mark.asyncio
async def test_unbound_box_does_not_authorize_an_empty_key():
    """A box adopted but not yet bound has no key; no presented key matches it."""
    hass = _hass(_keystore(_box(api_key=None)))
    resp = await _call(edge_api.EdgeMeView, "get", hass, api_key="")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_api_key_header_is_case_insensitive():
    hass = _hass(_keystore(_box()))
    view = edge_api.EdgeMeView()
    request = _FakeRequest(hass)
    request.headers["x-api-key"] = BOX_KEY
    resp = await view.get(request)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_health_needs_no_key():
    hass = _hass(_keystore(_box()))
    resp = await _call(edge_api.EdgeHealthView, "get", hass)
    assert resp.status == 200
    body = _json(resp)
    assert body["status"] == "ok"
    assert body["timestamp"].endswith("Z")


@pytest.mark.asyncio
async def test_me_names_the_box_and_the_household():
    hass = _hass(_keystore(_box(), _box(device_id="EC-2", api_key="box-api-key-2")))
    resp = await _call(edge_api.EdgeMeView, "get", hass, api_key="box-api-key-2")
    assert resp.status == 200
    assert _json(resp) == {
        "executorType": "edge",
        "deviceId": "EC-2",
        "householdId": ENTRY_ID,
    }


@pytest.mark.asyncio
async def test_bootstrap_is_404_without_key():
    hass = _hass(_keystore(_box()))
    resp = await _call(edge_api.EdgeBootstrapView, "post", hass)
    assert resp.status == 404
    assert _json(resp) == {"error": "island_no_bootstrap"}


@pytest.mark.asyncio
async def test_firmware_check_offers_nothing():
    hass = _hass(_keystore(_box()))
    resp = await _call(edge_api.EdgeFirmwareCheckView, "get", hass, api_key=BOX_KEY)
    assert resp.status == 200
    assert _json(resp) == {"offer": None, "reason": "island"}


@pytest.mark.asyncio
async def test_mqtt_token_is_404_island_no_mqtt():
    hass = _hass(_keystore(_box()))
    resp = await _call(
        edge_api.EdgeMqttTokenView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"device_id": "EC-1"},
    )
    assert resp.status == 404
    assert _json(resp) == {"error": "island_no_mqtt"}


@pytest.mark.asyncio
async def test_coredump_is_not_stored_and_logs_one_warning(caplog):
    hass = _hass(_keystore(_box()))
    with caplog.at_level(logging.WARNING, logger=edge_api.__name__):
        resp = await _call(
            edge_api.EdgeCoredumpView,
            "post",
            hass,
            api_key=BOX_KEY,
            body={"resetReason": "panic", "crashSummary": "LoadProhibited at 0x4200"},
        )
    assert resp.status == 200
    assert _json(resp) == {"stored": False, "reason": "island"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "panic" in warnings[0].getMessage()
    assert "LoadProhibited at 0x4200" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_coredump_with_a_body_that_is_not_json_still_answers_200():
    hass = _hass(_keystore(_box()))
    view = edge_api.EdgeCoredumpView()
    request = _FakeRequest(hass, api_key=BOX_KEY)

    async def _bad_json():
        raise ValueError("not json")

    request.json = _bad_json
    resp = await view.post(request)
    assert resp.status == 200
    assert _json(resp) == {"stored": False, "reason": "island"}


@pytest.mark.asyncio
async def test_pair_attempts_answers_empty():
    hass = _hass(_keystore(_box()))
    resp = await _call(
        edge_api.EdgePairAttemptsView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"device_id": "EC-1"},
    )
    assert resp.status == 200
    assert _json(resp) == {}


@pytest.mark.asyncio
async def test_update_inverter_ip_stores_the_address():
    ks = _keystore(_box(), _box(device_id="EC-2", api_key="box-api-key-2"))
    hass = _hass(ks)
    resp = await _call(
        edge_api.EdgeUpdateInverterIpView,
        "post",
        hass,
        api_key=BOX_KEY,
        body={"inverterId": "inv-1", "ipAddress": "10.0.0.9"},
    )
    assert resp.status == 200
    assert _json(resp) == {}
    state = await ks.load()
    assert state.edge_boxes["EC-1"].inverters["inv-1"]["ipAddress"] == "10.0.0.9"
    assert state.edge_boxes["EC-2"].inverters["inv-1"]["ipAddress"] == "10.0.0.5"


@pytest.mark.asyncio
async def test_update_inverter_ip_for_an_unknown_inverter_changes_nothing():
    """Another box's inverter, or one never adopted, is not written; the box
    still gets a 2xx so its watchdog does not count a failure."""
    ks = _keystore(_box())
    hass = _hass(ks)
    before = await ks.load()
    resp = await _call(
        edge_api.EdgeUpdateInverterIpView,
        "post",
        hass,
        api_key=BOX_KEY,
        body={"inverterId": "inv-other", "ipAddress": "10.0.0.9"},
    )
    assert resp.status == 200
    assert _json(resp) == {}
    assert (await ks.load()).edge_boxes == before.edge_boxes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "match_info"),
    [
        (edge_api.EdgeZigbeeSinkView, {}),
        (edge_api.EdgeZigbeeSinkView, {"device_id": "EC-1"}),
        (edge_api.EdgeChargerSinkView, {}),
        (edge_api.EdgeChargerSinkView, {"command_id": "cmd-1"}),
    ],
    ids=["zigbee", "sensor-readings", "charger", "charger-ack"],
)
async def test_zigbee_and_charger_sinks_answer_empty(view_cls, match_info):
    hass = _hass(_keystore(_box()))
    resp = await _call(
        view_cls, "post", hass, api_key=BOX_KEY, body={"x": 1}, match_info=match_info
    )
    assert resp.status == 200
    assert _json(resp) == {}


def test_zigbee_sinks_cover_every_zigbee_path():
    urls = {edge_api.EdgeZigbeeSinkView.url, *edge_api.EdgeZigbeeSinkView.extra_urls}
    assert urls == {
        "/api/v1/edge-devices/zigbee/device-joined",
        "/api/v1/edge-devices/zigbee/state-update",
        "/api/v1/edge-devices/{device_id}/sensor-readings",
    }


def test_charger_sinks_cover_every_charger_path():
    urls = {edge_api.EdgeChargerSinkView.url, *edge_api.EdgeChargerSinkView.extra_urls}
    assert urls == {
        "/api/v3/executors/charger-state",
        "/api/v3/executors/charger-relay",
        "/api/v3/executors/chargers/commands/{command_id}/ack",
    }


def test_edge_views_use_the_cloud_paths_and_skip_ha_auth():
    expected = {
        edge_api.EdgeHealthView: "/health",
        edge_api.EdgeMeView: "/api/v3/me",
        edge_api.EdgeBootstrapView: "/api/v1/edge-devices/bootstrap",
        edge_api.EdgeFirmwareCheckView: "/api/v3/executors/firmware-check",
        edge_api.EdgeMqttTokenView: "/api/v3/edge-devices/{device_id}/mqtt-token",
        edge_api.EdgeCoredumpView: "/api/v1/edge-devices/coredump",
        edge_api.EdgePairAttemptsView: "/api/v1/edge-devices/{device_id}/pair-attempts",
        edge_api.EdgeUpdateInverterIpView: "/api/v1/edge-devices/update-inverter-ip",
    }
    for view_cls, url in expected.items():
        assert view_cls.url == url
        assert view_cls.requires_auth is False


# What register_views registered before the edge views existed.
_VIEWS_BEFORE_EDGE = {
    "SvitgridLiveView": "/api/svitgrid/live",
    "SvitgridTodayView": "/api/svitgrid/today",
    "SvitgridHistoryView": "/api/svitgrid/history",
    "SvitgridSyncStatusView": "/api/svitgrid/sync-status",
    "SvitgridReadNowView": "/api/svitgrid/read-now",
    "SvitgridSettingsView": "/api/svitgrid/settings",
    "SvitgridOnboardingSnapshotView": "/api/svitgrid/onboarding-snapshot",
    "SvitgridHealthView": "/api/svitgrid/health",
    "SvitgridCommandsView": "/api/svitgrid/commands",
    "SvitgridTrustKeyView": "/api/svitgrid/trust-key",
    "SvitgridTrustKeyDetailView": "/api/svitgrid/trust-key/{signing_key_id}",
    "SvitgridEventsView": "/api/svitgrid/events",
    "SvitgridEventDetailView": "/api/svitgrid/events/{event_id}",
    "SvitgridCadenceView": "/api/svitgrid/cadence",
    "SvitgridSettlementInputView": "/api/svitgrid/settlement-input",
    "SvitgridIslandDevicesView": "/api/svitgrid/island-devices",
    "SvitgridIslandDeviceRevokeView": "/api/svitgrid/island-devices/revoke",
    "SvitgridLocalPresetsView": "/api/svitgrid/local-presets",
    "SvitgridPairLocalView": "/api/svitgrid/pair-local",
    "SvitgridIslandCloudCopyView": "/api/svitgrid/island/cloud-copy",
    "SvitgridIslandLeaveView": "/api/svitgrid/island/leave",
    "SvitgridKeysRevokeView": "/api/svitgrid/keys/revoke",
    "SvitgridIslandStatusView": "/api/svitgrid/island/status",
}


def _registered(hass) -> list:
    return [c.args[0] for c in hass.http.register_view.call_args_list]


def test_register_views_registers_the_edge_views():
    hass = MagicMock()
    hass.data = {}
    register_views(hass, MagicMock())
    registered = {type(v) for v in _registered(hass)}
    for view_cls, _method, _match in EDGE_VIEWS:
        assert view_cls in registered


def test_existing_views_unchanged_by_edge_registration():
    hass = MagicMock()
    hass.data = {}
    register_views(hass, MagicMock())
    by_name = {type(v).__name__: v for v in _registered(hass)}
    for name, url in _VIEWS_BEFORE_EDGE.items():
        assert name in by_name, name
        assert by_name[name].url == url
        assert list(getattr(by_name[name], "extra_urls", [])) == []
    edge_urls = set()
    for view_cls, _method, _match in EDGE_VIEWS:
        edge_urls |= {view_cls.url, *view_cls.extra_urls}
    assert not edge_urls & set(_VIEWS_BEFORE_EDGE.values())


# ── Task 6: the config block and sync-settings (svitgrid#1204) ──────────

_INV_1 = {
    "ipAddress": "10.0.0.5",
    "loggerSerial": "2712345678",
    "modelId": "deye_sg04lp3",
    "protocolId": "deye_3phase",
    "phases": 3,
    "hasBattery": True,
    "slaveId": 1,
    "pollingInterval": 60000,
    "reportingInterval": 60000,
}
_INV_2 = {**_INV_1, "ipAddress": "10.0.0.6", "loggerSerial": "2712345679"}

TASK6_VIEWS = [
    (edge_api.EdgeSyncSettingsView, "post", {}),
]


class _FakeReadingStore:
    def __init__(self, snapshots=None):
        self.snapshots = dict(snapshots or {})

    async def set_settings_snapshot(self, snapshot):
        self.snapshots[snapshot["inverter_id"]] = snapshot

    async def get_settings_snapshot(self, inverter_id):
        return self.snapshots.get(inverter_id)


def _hass_with_store(keystore, store):
    hass = _hass(keystore)
    hass.data[DOMAIN][ENTRY_ID] = {"store": store}
    return hass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), TASK6_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_config_and_sync_settings_are_404_without_adopted_boxes(view_cls, method, match_info):
    for hass in (_hass(_keystore()), _hass(None)):
        resp = await _call(view_cls, method, hass, api_key=BOX_KEY, match_info=match_info)
        assert resp.status == 404
        assert _json(resp) == {"error": "no_edge_boxes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), TASK6_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_config_and_sync_settings_unknown_key_is_401_not_410(view_cls, method, match_info):
    hass = _hass(_keystore(_box()))
    for api_key in (None, "", "not-a-bound-key"):
        resp = await _call(view_cls, method, hass, api_key=api_key, match_info=match_info)
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}


def test_config_and_sync_settings_use_the_cloud_paths_and_are_registered():
    assert edge_api.EdgeSyncSettingsView.url == "/api/v1/edge-devices/sync-settings"
    hass = MagicMock()
    hass.data = {}
    register_views(hass, MagicMock())
    registered = {type(v) for v in _registered(hass)}
    for view_cls, _method, _match in TASK6_VIEWS:
        assert view_cls.requires_auth is False
        assert view_cls in registered


@pytest.mark.asyncio
async def test_sync_settings_stores_snapshot_with_read_at():
    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    body = {
        "inverterId": "inv-1",
        "modelId": "deye_sg04lp3",
        "startRegister": 115,
        "registers": [1, 2, 3],
    }
    resp = await _call(edge_api.EdgeSyncSettingsView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    assert _json(resp) == {"ok": True}

    stored = store.snapshots["inv-1"]
    assert stored["inverter_id"] == "inv-1"
    assert stored["model_id"] == "deye_sg04lp3"
    assert stored["start_register"] == 115
    assert stored["registers"] == [1, 2, 3]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stored["read_at"])
    assert settings_sync.find_settings_snapshot(hass, "inv-1") == stored


@pytest.mark.asyncio
async def test_sync_settings_stores_a_brand_body_as_its_kind():
    store = _FakeReadingStore()
    inverters = {"inv-lux": {**_INV_1, "modelId": "luxpower_sna_5000_wpv"}}
    hass = _hass_with_store(_keystore(_box(inverters=inverters)), store)
    blocks = [{"startRegister": 0, "registers": [7] * 40}]
    body = {
        "inverterId": "inv-lux",
        "modelId": "luxpower_sna_5000_wpv",
        "protocol": "luxpower",
        "familyCode": "AAAA",
        "blocks": blocks,
    }
    resp = await _call(edge_api.EdgeSyncSettingsView, "post", hass, api_key=BOX_KEY, body=body)
    assert _json(resp) == {"ok": True}
    stored = store.snapshots["inv-lux"]
    assert stored["kind"] == "luxpower"
    assert stored["body"] == {"protocol": "luxpower", "familyCode": "AAAA", "blocks": blocks}
    served = settings_sync.settings_snapshot_response(stored)
    assert served == {
        "inverterId": "inv-lux",
        "modelId": "luxpower_sna_5000_wpv",
        "protocol": "luxpower",
        "familyCode": "AAAA",
        "blocks": blocks,
        "readAt": stored["read_at"],
    }


@pytest.mark.asyncio
async def test_sync_settings_unknown_inverter_is_200_not_ok():
    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    body = {"inverterId": "inv-x", "modelId": "deye_sg04lp3", "startRegister": 115, "registers": []}
    resp = await _call(edge_api.EdgeSyncSettingsView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    assert _json(resp) == {"ok": False, "error": "unknown_inverter"}
    assert store.snapshots == {}
    assert settings_sync.find_settings_snapshot(hass, "inv-x") is None


@pytest.mark.asyncio
async def test_sync_settings_inverter_of_another_box_is_unknown():
    store = _FakeReadingStore()
    other = _box(device_id="EC-2", api_key="other-key", inverters={"inv-2": _INV_2})
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1}), other), store)
    body = {"inverterId": "inv-2", "modelId": "deye_sg04lp3", "startRegister": 115, "registers": []}
    resp = await _call(edge_api.EdgeSyncSettingsView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    assert _json(resp) == {"ok": False, "error": "unknown_inverter"}
    assert store.snapshots == {}


@pytest.mark.asyncio
async def test_sync_settings_model_mismatch_is_200_not_ok():
    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    body = {"inverterId": "inv-1", "modelId": "deye_sg01hp3", "startRegister": 115, "registers": []}
    resp = await _call(edge_api.EdgeSyncSettingsView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    assert _json(resp) == {"ok": False, "error": "model_mismatch"}
    assert store.snapshots == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"modelId": "deye_sg04lp3"},
        {"inverterId": "inv-1"},
        {"inverterId": "inv-1", "modelId": "deye_sg04lp3", "startRegister": 115},
        {"inverterId": "inv-1", "modelId": "deye_sg04lp3", "registers": "x"},
    ],
)
async def test_sync_settings_bad_body_is_200_not_ok(body):
    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    resp = await _call(edge_api.EdgeSyncSettingsView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    assert _json(resp)["ok"] is False
    assert store.snapshots == {}


def test_config_block_lists_every_adopted_inverter():
    box = _box(inverters={"inv-1": _INV_1, "inv-2": _INV_2})
    block = edge_api.build_edge_config(box)
    assert [inv["inverterId"] for inv in block["inverters"]] == ["inv-1", "inv-2"]
    assert block["inverters"][0] == {"inverterId": "inv-1", **_INV_1}
    assert block["inverters"][1] == {"inverterId": "inv-2", **_INV_2}
    assert block["pollingInterval"] == 60000
    assert block["reportingInterval"] == 60000
    assert block["nonMeteringModels"] == []
    assert block["chargers"] == []
    assert block["relayChargers"] == []
    assert re.fullmatch(r"[0-9a-f]{16}", block["configHash"])


def test_config_block_lists_an_inverter_with_no_adopt_details():
    """The reconcile prunes every inverter the block does not list, so an id
    in `inverter_ids` is listed even before its adopt details are known."""
    box = EdgeBoxEntry(
        device_id="EC-1",
        api_key=BOX_KEY,
        inverter_ids=["inv-1", "inv-bare"],
        inverters={"inv-1": _INV_1},
        added_at="2026-10-28T10:00:00+00:00",
        pair_code_hash=None,
        pair_code_expires_at=None,
    )
    block = edge_api.build_edge_config(box)
    assert [inv["inverterId"] for inv in block["inverters"]] == ["inv-1", "inv-bare"]
    assert block["inverters"][1] == {"inverterId": "inv-bare"}


def test_config_hash_is_stable_for_same_inverters():
    first = edge_api.build_edge_config(_box(inverters={"inv-1": _INV_1, "inv-2": _INV_2}))
    again = edge_api.build_edge_config(
        _box(inverters={"inv-1": dict(reversed(list(_INV_1.items()))), "inv-2": _INV_2})
    )
    assert first["configHash"] == again["configHash"]
    moved = edge_api.build_edge_config(
        _box(inverters={"inv-1": {**_INV_1, "ipAddress": "10.0.0.9"}, "inv-2": _INV_2})
    )
    assert moved["configHash"] != first["configHash"]


@pytest.mark.asyncio
async def test_sync_settings_inflates_a_gzip_body():
    """The box gzips every body of 256 bytes or more
    (`HTTP_WORKER_COMPRESS_MIN_BYTES`), so every Deye settings body arrives
    gzipped. A body that still carries the gzip bytes is inflated here."""
    import gzip
    import json

    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    body = {
        "inverterId": "inv-1",
        "modelId": "deye_sg04lp3",
        "startRegister": 115,
        "registers": list(range(77)),
    }
    request = _FakeRequest(hass, api_key=BOX_KEY)
    request.headers["Content-Encoding"] = "gzip"
    raw = gzip.compress(json.dumps(body).encode())

    async def _read():
        return raw

    async def _no_json():
        raise ValueError("gzip body is not JSON")

    request.read = _read
    request.json = _no_json
    resp = await edge_api.EdgeSyncSettingsView().post(request)
    assert _json(resp) == {"ok": True}
    assert store.snapshots["inv-1"]["registers"] == list(range(77))


@pytest.mark.asyncio
async def test_a_gzip_body_aiohttp_already_inflated_is_read_as_json():
    """Bench 2026-10-30: Home Assistant's aiohttp inflates a gzip request
    body itself (`auto_decompress`) and keeps the `Content-Encoding: gzip`
    header, so the view reads plain JSON under a gzip header. Inflating it
    again failed, and every reading the box sent got 400."""
    import json

    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    body = {
        "inverterId": "inv-1",
        "modelId": "deye_sg04lp3",
        "startRegister": 115,
        "registers": list(range(77)),
    }
    request = _FakeRequest(hass, api_key=BOX_KEY)
    request.headers["Content-Encoding"] = "gzip"
    raw = json.dumps(body).encode()

    async def _read():
        return raw

    request.read = _read
    resp = await edge_api.EdgeSyncSettingsView().post(request)
    assert _json(resp) == {"ok": True}
    assert store.snapshots["inv-1"]["registers"] == list(range(77))


@pytest.mark.asyncio
async def test_sync_settings_refuses_a_gzip_body_that_inflates_too_far():
    import gzip

    store = _FakeReadingStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    request = _FakeRequest(hass, api_key=BOX_KEY)
    request.headers["Content-Encoding"] = "gzip"
    raw = gzip.compress(b" " * (edge_api.MAX_INFLATED_BODY_BYTES + 1))

    async def _read():
        return raw

    request.read = _read
    resp = await edge_api.EdgeSyncSettingsView().post(request)
    assert resp.status == 200
    assert _json(resp) == {"ok": False, "error": "bad_body"}
    assert store.snapshots == {}


# ── Task 3: the ingest views and the box lifecycle (svitgrid#1205) ──────

TASK3_VIEWS = [
    (edge_api.EdgeIngestBatchView, "post", {}),
    (edge_api.EdgeIngestReadingView, "post", {}),
]


class _FakeIngestStore(_FakeReadingStore):
    def __init__(self):
        super().__init__()
        self.appended: list[dict] = []
        self.box_states: dict[str, dict] = {}

    async def append(self, reading):
        self.appended.append(reading)

    async def live_snapshot(self):
        latest = {}
        for reading in self.appended:
            inverter_id = reading["inverterId"]
            if (
                inverter_id not in latest
                or reading["timestamp"] >= latest[inverter_id]["timestamp"]
            ):
                latest[inverter_id] = reading
        return [
            {"inverterId": k, "ts": v["timestamp"], "payload": v, "intervalS": None}
            for k, v in latest.items()
        ]

    async def set_edge_box_state(self, device_id, state):
        self.box_states[device_id] = state

    async def get_edge_box_state(self, device_id):
        return self.box_states.get(device_id)


class _FakeEdgeQueue:
    def __init__(self, pending: dict[str, int]):
        self.pending = pending

    async def async_pending_count(self, device_id):
        return self.pending.get(device_id, 0)


_PIPELINE_CASES = Path(__file__).parent / "fixtures" / "island_pipeline_cases.jsonl"
# A complete battery Deye reading: the shared pipeline fixture's first case.
_CASE_0_PAYLOAD = json.loads(_PIPELINE_CASES.read_text().splitlines()[0])["payload"]


def _reading(inverter_id="inv-1", ts="2026-10-28T10:00:00.000Z", **extra) -> dict:
    return {**_CASE_0_PAYLOAD, "inverterId": inverter_id, "timestamp": ts, **extra}


def _stored(readings, inverters=None) -> list[dict]:
    """What the pipeline stores for `readings`, in order, each derived from
    the previous stored reading of its inverter."""
    from custom_components.svitgrid import edge_reading_pipeline

    inverters = inverters or {"inv-1": _INV_1, "inv-2": _INV_2}
    latest: dict[str, dict] = {}
    out = []
    for reading in readings:
        inverter_id = reading["inverterId"]
        result = edge_reading_pipeline.process(
            reading, inverters[inverter_id], latest.get(inverter_id), now=datetime.now(UTC)
        )
        assert result.store_data is not None, result.rejected
        latest[inverter_id] = result.store_data
        out.append(result.store_data)
    return out


_SERVER_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), TASK3_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_ingest_views_are_404_without_adopted_boxes(view_cls, method, match_info):
    for hass in (_hass(_keystore()), _hass(None)):
        for api_key in (None, BOX_KEY):
            resp = await _call(
                view_cls, method, hass, api_key=api_key, match_info=match_info, body=_reading()
            )
            assert resp.status == 404
            assert _json(resp) == {"error": "no_edge_boxes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), TASK3_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_ingest_unknown_key_is_401_not_410(view_cls, method, match_info):
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    for api_key in (None, "", "not-a-bound-key"):
        resp = await _call(
            view_cls, method, hass, api_key=api_key, match_info=match_info, body=_reading()
        )
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}
    assert store.appended == []


def test_ingest_views_use_the_cloud_paths_and_are_registered():
    assert edge_api.EdgeIngestBatchView.url == "/api/v1/ingest/readings"
    assert edge_api.EdgeIngestReadingView.url == "/api/v1/ingest/reading"
    hass = MagicMock()
    hass.data = {}
    register_views(hass, MagicMock())
    registered = {type(v) for v in _registered(hass)}
    for view_cls, _method, _match in TASK3_VIEWS:
        assert view_cls.requires_auth is False
        assert view_cls in registered


@pytest.mark.asyncio
async def test_batch_stores_each_known_reading():
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1, "inv-2": _INV_2})), store)
    readings = [
        _reading("inv-1", "2026-10-28T10:00:00.000Z"),
        _reading("inv-2", "2026-10-28T10:00:01.000Z"),
        _reading("inv-1", "2026-10-28T10:01:00.000Z"),
    ]
    resp = await _call(
        edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body={"readings": readings}
    )
    assert resp.status == 200
    body = _json(resp)
    assert body["results"] == [
        {"ok": True, "inverterId": "inv-1"},
        {"ok": True, "inverterId": "inv-2"},
        {"ok": True, "inverterId": "inv-1"},
    ]
    assert store.appended == _stored(readings)
    assert body["pollIntervalMs"] == 30000
    assert body["mqttPublishReadings"] is False
    assert body["ingestMode"] == "batch"
    assert body["expectedInverterCount"] == 2
    assert _SERVER_TIME.fullmatch(body["serverTime"])
    assert "dropped" not in body
    assert "stopped" not in body


@pytest.mark.asyncio
async def test_batch_runs_each_reading_through_the_pipeline(monkeypatch):
    """The pipeline (Task 5) decides what is stored; a reading it refuses is
    a per-item 422, as the cloud answers it."""
    from custom_components.svitgrid import edge_reading_pipeline

    calls = []

    def _process(reading, ctx, prev, *, now):
        calls.append((reading["inverterId"], ctx))
        if reading["batterySoc"] > 100:
            return edge_reading_pipeline.PipelineResult(None, "soc_out_of_range", [], {})
        return edge_reading_pipeline.PipelineResult({**reading, "derived": True}, None, [], {})

    monkeypatch.setattr(edge_reading_pipeline, "process", _process)
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    readings = [_reading(), _reading(ts="2026-10-28T10:01:00.000Z", batterySoc=140)]
    resp = await _call(
        edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body={"readings": readings}
    )
    assert resp.status == 200
    assert _json(resp)["results"] == [
        {"ok": True, "inverterId": "inv-1"},
        {
            "ok": False,
            "inverterId": "inv-1",
            "error": "Reading rejected",
            "reason": "soc_out_of_range",
            "status": 422,
        },
    ]
    assert store.appended == [{**readings[0], "derived": True}]
    assert calls == [("inv-1", _INV_1), ("inv-1", _INV_1)]


@pytest.mark.asyncio
async def test_batch_answers_what_the_real_pipeline_refuses_per_item():
    """A reading the cloud's schema refuses is a per-item 400 and one its
    validator refuses a 422 with the reason; the rest of the batch is stored."""
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    no_voltage = {k: v for k, v in _reading().items() if k != "batteryVoltage"}
    impossible = _reading(ts="2026-10-28T10:01:00.000Z", loadPower=9000)
    stored = _reading(ts="2026-10-28T10:02:00.000Z")
    resp = await _call(
        edge_api.EdgeIngestBatchView,
        "post",
        hass,
        api_key=BOX_KEY,
        body={"readings": [no_voltage, impossible, stored]},
    )
    assert resp.status == 200
    results = _json(resp)["results"]
    assert results[0] == {
        "ok": False,
        "inverterId": "inv-1",
        "error": "Validation error",
        "status": 400,
    }
    assert results[1]["ok"] is False
    assert results[1]["status"] == 422
    assert results[1]["reason"].startswith("energyBalance: loadPower=9000W")
    assert results[2] == {"ok": True, "inverterId": "inv-1"}
    assert store.appended == _stored([stored])


@pytest.mark.asyncio
async def test_unknown_inverter_item_is_404_not_whole_batch():
    """Review focus 2: a box with two inverters, one not adopted. Only that
    item fails; the other is stored and the answer stays 200, so the box
    drops only that item (`parse_batch_response_results_ex`)."""
    store = _FakeIngestStore()
    other = _box(device_id="EC-2", api_key="other-key", inverters={"inv-2": _INV_2})
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1}), other), store)
    readings = [_reading("inv-1"), _reading("inv-x"), _reading("inv-2")]
    resp = await _call(
        edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body={"readings": readings}
    )
    assert resp.status == 200
    assert _json(resp)["results"] == [
        {"ok": True, "inverterId": "inv-1"},
        {"ok": False, "inverterId": "inv-x", "error": "Inverter not found", "status": 404},
        {"ok": False, "inverterId": "inv-2", "error": "Inverter not found", "status": 404},
    ]
    assert store.appended == _stored([readings[0]])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [None, "x", {"inverterId": "inv-1"}, {"timestamp": "2026-10-28T10:00:00Z"}],
)
async def test_invalid_item_is_400_not_whole_batch(item):
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    body = {"readings": [item, _reading()]}
    resp = await _call(edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    results = _json(resp)["results"]
    assert results[0]["ok"] is False
    assert results[0]["status"] == 400
    assert results[1] == {"ok": True, "inverterId": "inv-1"}
    assert store.appended == _stored([_reading()])


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["x", [], {"readings": "x"}])
async def test_batch_with_a_bad_body_is_400_not_410(body):
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    resp = await _call(edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 400
    assert store.appended == []


@pytest.mark.asyncio
async def test_gzip_body_is_inflated():
    import gzip
    import json

    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    readings = [_reading(ts=f"2026-10-28T10:{m:02d}:00.000Z") for m in range(10)]
    request = _FakeRequest(hass, api_key=BOX_KEY)
    request.headers["Content-Encoding"] = "gzip"
    raw = gzip.compress(json.dumps({"readings": readings}).encode())

    async def _read():
        return raw

    async def _no_json():
        raise ValueError("gzip body is not JSON")

    request.read = _read
    request.json = _no_json
    resp = await edge_api.EdgeIngestBatchView().post(request)
    assert resp.status == 200
    assert len(_json(resp)["results"]) == 10
    assert store.appended == _stored(readings)


@pytest.mark.asyncio
async def test_poll_interval_is_5000_with_pending_commands():
    store = _FakeIngestStore()
    other = _box(device_id="EC-2", api_key="other-key", inverters={"inv-2": _INV_2})
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1}), other), store)
    hass.data[DOMAIN][ENTRY_ID]["edge_queue"] = _FakeEdgeQueue({"EC-1": 2})
    body = {"readings": [_reading()]}
    resp = await _call(edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body=body)
    assert _json(resp)["pollIntervalMs"] == 5000

    resp = await _call(
        edge_api.EdgeIngestBatchView, "post", hass, api_key="other-key", body={"readings": []}
    )
    assert _json(resp)["pollIntervalMs"] == 30000


@pytest.mark.asyncio
async def test_telemetry_updates_box_state():
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    telemetry = {
        "version": "2.172.0",
        "uptime": 3600,
        "wifiRssi": -55,
        "otaStatus": "idle",
        "freeHeap": 145000,
    }
    body = {
        "readings": [_reading()],
        "telemetry": telemetry,
        "bms": [{"soc": 1}],
        "socketReadings": [{"x": 1}],
    }
    resp = await _call(edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 200
    state = store.box_states["EC-1"]
    assert {k: state[k] for k in ("version", "uptime", "wifiRssi", "otaStatus")} == {
        "version": "2.172.0",
        "uptime": 3600,
        "wifiRssi": -55,
        "otaStatus": "idle",
    }
    assert "freeHeap" not in state
    assert _SERVER_TIME.fullmatch(state["lastContactAt"])
    assert set(store.box_states) == {"EC-1"}


@pytest.mark.asyncio
async def test_every_ingest_stamps_last_contact_and_keeps_telemetry():
    store = _FakeIngestStore()
    store.box_states["EC-1"] = {
        "version": "2.172.0",
        "lastContactAt": "2026-10-28T09:00:00.000Z",
    }
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    resp = await _call(
        edge_api.EdgeIngestReadingView, "post", hass, api_key=BOX_KEY, body=_reading()
    )
    assert resp.status == 200
    state = store.box_states["EC-1"]
    assert state["version"] == "2.172.0"
    assert state["lastContactAt"] != "2026-10-28T09:00:00.000Z"
    assert _SERVER_TIME.fullmatch(state["lastContactAt"])


@pytest.mark.asyncio
async def test_single_reading_path_answers_success():
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    resp = await _call(
        edge_api.EdgeIngestReadingView, "post", hass, api_key=BOX_KEY, body=_reading()
    )
    assert resp.status == 200
    body = _json(resp)
    assert body["success"] is True
    assert body["stored"] is True
    assert body["mqttPublishReadings"] is False
    assert _SERVER_TIME.fullmatch(body["serverTime"])
    assert "stopped" not in body
    assert store.appended == _stored([_reading()])


@pytest.mark.asyncio
async def test_single_reading_for_an_unknown_inverter_is_404_not_410():
    store = _FakeIngestStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    resp = await _call(
        edge_api.EdgeIngestReadingView, "post", hass, api_key=BOX_KEY, body=_reading("inv-x")
    )
    assert resp.status == 404
    assert _json(resp) == {"error": "Inverter not found"}
    assert store.appended == []


@pytest.mark.asyncio
async def test_ingest_without_a_reading_store_is_503_so_the_box_keeps_its_readings():
    hass = _hass(_keystore(_box(inverters={"inv-1": _INV_1})))
    resp = await _call(
        edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body={"readings": [_reading()]}
    )
    assert resp.status == 503


# ── Task 4: the command poll and ack views (svitgrid#1206) ──────────────

TASK4_VIEWS = [
    (edge_api.EdgeCommandsPollView, "get", {}),
    (edge_api.EdgeCommandAckView, "post", {"command_id": "cmd-1"}),
]


def _hass_with_queue(keystore, queue):
    hass = _hass(keystore)
    hass.data[DOMAIN][ENTRY_ID] = {"store": _FakeIngestStore(), "edge_queue": queue}
    return hass


def _real_queue(tmp_path):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    return EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)


async def _poll(hass, *, api_key=BOX_KEY, query=None):
    view = edge_api.EdgeCommandsPollView()
    request = _FakeRequest(hass, api_key=api_key)
    request.query = query or {"wait": "0"}
    return await view.get(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), TASK4_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_poll_and_ack_are_404_without_adopted_boxes(view_cls, method, match_info):
    for hass in (_hass(_keystore()), _hass(None)):
        for api_key in (None, BOX_KEY):
            resp = await _call(view_cls, method, hass, api_key=api_key, match_info=match_info)
            assert resp.status == 404
            assert _json(resp) == {"error": "no_edge_boxes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_cls", "method", "match_info"), TASK4_VIEWS, ids=lambda v: getattr(v, "__name__", None)
)
async def test_poll_and_ack_unknown_key_is_401_not_410(view_cls, method, match_info, tmp_path):
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1})), _real_queue(tmp_path))
    for api_key in (None, "", "not-a-bound-key"):
        resp = await _call(view_cls, method, hass, api_key=api_key, match_info=match_info)
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}


def test_poll_and_ack_use_the_cloud_paths_and_are_registered():
    assert edge_api.EdgeCommandsPollView.url == "/api/v3/executors/commands"
    assert edge_api.EdgeCommandAckView.url == "/api/v3/executors/commands/{command_id}/ack"
    hass = MagicMock()
    hass.data = {}
    register_views(hass, MagicMock())
    registered = {type(v) for v in _registered(hass)}
    for view_cls, _method, _match in TASK4_VIEWS:
        assert view_cls.requires_auth is False
        assert view_cls in registered


@pytest.mark.asyncio
async def test_poll_returns_one_pending_with_signing_fields(tmp_path):
    queue = _real_queue(tmp_path)
    box = _box(inverters={"inv-1": _INV_1})
    hass = _hass_with_queue(_keystore(box), queue)
    signing = {"signedEventData": '{"a":1}', "signature": "MEUC", "signingKeyId": "phone-1"}
    cid = await queue.async_enqueue(
        "EC-1", "set_work_mode", {"mode": 1}, signing=signing, inverter_id="inv-1", source="t"
    )
    await queue.async_enqueue(
        "EC-1", "read_inverter_settings", {}, signing=None, inverter_id="inv-1", source="t"
    )

    resp = await _poll(hass)
    assert resp.status == 200
    body = _json(resp)
    assert body["commands"] == [
        {
            "id": cid,
            "command": "set_work_mode",
            "payload": {"mode": 1},
            "inverterId": "inv-1",
            **signing,
        }
    ]
    assert _SERVER_TIME.fullmatch(body["serverTime"])
    assert body["executorType"] == "edge"
    assert body["ingestMode"] == "batch"
    assert body["periodicPollDisabled"] is False
    assert body["config"] == edge_api.build_edge_config(box)

    unsigned = _json(await _poll(hass))["commands"]
    assert len(unsigned) == 1
    assert unsigned[0]["command"] == "read_inverter_settings"
    assert "signature" not in unsigned[0]
    assert "signedEventData" not in unsigned[0]
    assert "signingKeyId" not in unsigned[0]


@pytest.mark.asyncio
async def test_poll_with_nothing_pending_is_200_empty(tmp_path):
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1})), _real_queue(tmp_path))
    resp = await _poll(hass)
    assert resp.status == 200
    body = _json(resp)
    assert body["commands"] == []
    assert body["pollIntervalMs"] == edge_api.POLL_INTERVAL_IDLE_MS


@pytest.mark.asyncio
async def test_poll_without_a_queue_is_200_empty():
    """Before the queue is wired into setup (Task 8) the box still gets a 2xx."""
    hass = _hass(_keystore(_box(inverters={"inv-1": _INV_1})))
    resp = await _poll(hass)
    assert resp.status == 200
    assert _json(resp)["commands"] == []


@pytest.mark.asyncio
async def test_poll_serves_only_the_calling_box(tmp_path):
    queue = _real_queue(tmp_path)
    other = _box(device_id="EC-2", api_key="box-api-key-2", inverters={"inv-9": _INV_2})
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1}), other), queue)
    await queue.async_enqueue("EC-2", "a", {}, signing=None, inverter_id="inv-9", source="t")
    assert _json(await _poll(hass))["commands"] == []
    assert len(_json(await _poll(hass, api_key="box-api-key-2"))["commands"]) == 1


@pytest.mark.asyncio
async def test_poll_omits_config_on_matching_hash(tmp_path):
    box = _box(inverters={"inv-1": _INV_1})
    hass = _hass_with_queue(_keystore(box), _real_queue(tmp_path))
    config_hash = edge_api.build_edge_config(box)["configHash"]
    body = _json(await _poll(hass, query={"wait": "0", "configHash": config_hash}))
    assert "config" not in body
    stale = _json(await _poll(hass, query={"wait": "0", "configHash": "0000000000000000"}))
    assert stale["config"]["configHash"] == config_hash


@pytest.mark.asyncio
async def test_poll_interval_5000_while_pending(tmp_path):
    queue = _real_queue(tmp_path)
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1})), queue)
    for name in ("a", "b"):
        await queue.async_enqueue("EC-1", name, {}, signing=None, inverter_id=None, source="t")
    assert _json(await _poll(hass))["pollIntervalMs"] == edge_api.POLL_INTERVAL_PENDING_MS
    assert _json(await _poll(hass))["pollIntervalMs"] == edge_api.POLL_INTERVAL_IDLE_MS


@pytest.mark.asyncio
async def test_ack_unknown_id_is_200(tmp_path):
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1})), _real_queue(tmp_path))
    resp = await _call(
        edge_api.EdgeCommandAckView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"command_id": "no-such-id"},
        body={"success": True, "executorVersion": "2.172.0"},
    )
    assert resp.status == 200
    assert _json(resp) == {"success": False, "status": "unknown"}


@pytest.mark.asyncio
async def test_ack_without_a_queue_is_200_unknown():
    hass = _hass(_keystore(_box(inverters={"inv-1": _INV_1})))
    resp = await _call(
        edge_api.EdgeCommandAckView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"command_id": "cmd-1"},
        body={"success": True},
    )
    assert resp.status == 200
    assert _json(resp) == {"success": False, "status": "unknown"}


@pytest.mark.asyncio
async def test_ack_records_the_outcome(tmp_path):
    queue = _real_queue(tmp_path)
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1})), queue)
    ok = await queue.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="t")
    bad = await queue.async_enqueue("EC-1", "b", {}, signing=None, inverter_id=None, source="t")
    await _poll(hass)
    await _poll(hass)

    resp = await _call(
        edge_api.EdgeCommandAckView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"command_id": ok},
        body={"success": True, "result": {"x": 1}, "executorVersion": "2.172.0"},
    )
    assert resp.status == 200
    assert _json(resp) == {"success": True, "status": "success"}

    resp = await _call(
        edge_api.EdgeCommandAckView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"command_id": bad},
        body={"success": False, "rejected": True, "reason": "bad_signature"},
    )
    assert resp.status == 200
    assert _json(resp) == {"success": True, "status": "failed"}
    rows = {r["id"]: r for r in await queue.async_recent("EC-1")}
    assert rows[ok]["result"] == {"x": 1}
    assert rows[bad]["reason"] == "bad_signature"


@pytest.mark.asyncio
async def test_ack_of_another_boxs_command_is_200_unknown(tmp_path):
    queue = _real_queue(tmp_path)
    other = _box(device_id="EC-2", api_key="box-api-key-2", inverters={"inv-9": _INV_2})
    hass = _hass_with_queue(_keystore(_box(inverters={"inv-1": _INV_1}), other), queue)
    cid = await queue.async_enqueue("EC-2", "a", {}, signing=None, inverter_id=None, source="t")
    await _poll(hass, api_key="box-api-key-2")
    resp = await _call(
        edge_api.EdgeCommandAckView,
        "post",
        hass,
        api_key=BOX_KEY,
        match_info={"command_id": cid},
        body={"success": True},
    )
    assert _json(resp) == {"success": False, "status": "unknown"}
    assert (await queue.async_recent("EC-2"))[0]["state"] == "delivered"


# ── Task 7: grid events and the edge-confirmed precedence (svitgrid#1208) ──


class _FakeGridStore(_FakeIngestStore):
    """The ingest fake with the reading store's grid-event table."""

    def __init__(self):
        super().__init__()
        self.grid_events: list[dict] = []

    async def add_grid_event(self, event):
        if any(
            e["inverterId"] == event["inverterId"] and e["confirmedAt"] == event["confirmedAt"]
            for e in self.grid_events
        ):
            return False
        self.grid_events.append(event)
        return True

    async def latest_grid_event(self, inverter_id):
        events = [e for e in self.grid_events if e["inverterId"] == inverter_id]
        return max(events, key=lambda e: e["confirmedAt"]) if events else None


def _grid_event(state="lost", confirmed_at=None, inverter_id="inv-1", **signals) -> dict:
    return {
        "inverterId": inverter_id,
        "state": state,
        "confirmedAt": confirmed_at or _server_time_offset(-30),
        "signals": signals or {"phaseVoltages": [0, 0, 0], "gridRelayClosed": 0},
    }


def _server_time_offset(seconds: int) -> str:
    at = _NOW + timedelta(seconds=seconds)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_grid_event_is_404_without_adopted_boxes():
    for hass in (_hass(_keystore()), _hass(None)):
        for api_key in (None, BOX_KEY):
            resp = await _call(
                edge_api.EdgeGridEventView, "post", hass, api_key=api_key, body=_grid_event()
            )
            assert resp.status == 404
            assert _json(resp) == {"error": "no_edge_boxes"}


@pytest.mark.asyncio
async def test_grid_event_unknown_key_is_401_not_410():
    store = _FakeGridStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    for api_key in (None, "", "not-a-bound-key"):
        resp = await _call(
            edge_api.EdgeGridEventView, "post", hass, api_key=api_key, body=_grid_event()
        )
        assert resp.status == 401
        assert _json(resp) == {"error": "unauthorized"}
    assert store.grid_events == []


def test_grid_event_uses_the_cloud_path_and_is_registered():
    assert edge_api.EdgeGridEventView.url == "/api/v3/executors/grid-event"
    assert edge_api.EdgeGridEventView.requires_auth is False
    hass = MagicMock()
    hass.data = {}
    register_views(hass, MagicMock())
    assert edge_api.EdgeGridEventView in {type(v) for v in _registered(hass)}


@pytest.mark.asyncio
async def test_grid_event_is_stored_and_deduped():
    store = _FakeGridStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    event = _grid_event(confirmed_at="2026-10-28T10:00:00.000Z")
    first = await _call(edge_api.EdgeGridEventView, "post", hass, api_key=BOX_KEY, body=event)
    assert first.status == 200
    assert _json(first) == {"ok": True, "deduped": False}
    again = await _call(edge_api.EdgeGridEventView, "post", hass, api_key=BOX_KEY, body=event)
    assert again.status == 200
    assert _json(again) == {"ok": True, "deduped": True}
    assert store.grid_events == [event]
    assert "lastContactAt" in store.box_states["EC-1"]


@pytest.mark.asyncio
async def test_grid_event_for_an_inverter_the_box_does_not_hold_is_403():
    """As the cloud answers it; the box drops the event on a 403
    (`grid_event_uploader.c`) instead of retrying."""
    store = _FakeGridStore()
    other = _box(device_id="EC-2", api_key="other-key", inverters={"inv-2": _INV_2})
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1}), other), store)
    resp = await _call(
        edge_api.EdgeGridEventView,
        "post",
        hass,
        api_key=BOX_KEY,
        body=_grid_event(inverter_id="inv-2"),
    )
    assert resp.status == 403
    assert _json(resp) == {"error": "inverter_not_owned"}
    assert store.grid_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("x", "invalid_body"),
        ({"state": "lost"}, "missing_inverterId"),
        ({"inverterId": "inv-1", "state": "x"}, "invalid_state"),
    ],
)
async def test_grid_event_with_a_bad_body_is_400_not_410(body, error):
    store = _FakeGridStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    resp = await _call(edge_api.EdgeGridEventView, "post", hass, api_key=BOX_KEY, body=body)
    assert resp.status == 400
    assert _json(resp) == {"error": error}
    assert store.grid_events == []


@pytest.mark.asyncio
async def test_a_lost_event_with_an_energized_grid_is_vetoed_and_not_stored():
    store = _FakeGridStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    event = _grid_event(phaseVoltages=[228, 231, 229], gridFrequency=50, gridRelayClosed=0)
    resp = await _call(edge_api.EdgeGridEventView, "post", hass, api_key=BOX_KEY, body=event)
    assert resp.status == 200
    assert _json(resp) == {"ok": True, "vetoed": True}
    assert store.grid_events == []


@pytest.mark.asyncio
async def test_a_lost_event_holds_the_grid_down_through_the_next_reading():
    """The box's confirmed `lost` wins over a reading that cannot see the
    grid (`shouldKeepEdgeConfirmed`), as on the cloud."""
    store = _FakeGridStore()
    hass = _hass_with_store(_keystore(_box(inverters={"inv-1": _INV_1})), store)
    up = _reading(ts=_server_time_offset(-120))
    await _call(
        edge_api.EdgeIngestBatchView, "post", hass, api_key=BOX_KEY, body={"readings": [up]}
    )
    assert store.appended[-1]["gridPresent"] is True

    event = _grid_event(confirmed_at=_server_time_offset(-60))
    await _call(edge_api.EdgeGridEventView, "post", hass, api_key=BOX_KEY, body=event)

    no_grid_signal = {
        k: v
        for k, v in _reading(ts=_server_time_offset(-30)).items()
        if k not in ("gridVoltage", "phaseVoltages", "gridFrequency", "gridRelayClosed")
    }
    resp = await _call(
        edge_api.EdgeIngestBatchView,
        "post",
        hass,
        api_key=BOX_KEY,
        body={"readings": [no_grid_signal]},
    )
    assert _json(resp)["results"] == [{"ok": True, "inverterId": "inv-1"}]
    stored = store.appended[-1]
    assert stored["gridPresent"] is False
    assert stored["gridConfirmedSource"] == "edge"
    assert stored["gridConfirmedAt"] == event["confirmedAt"]


# ── the last refusal, for GET /api/svitgrid/edge/status (Task 15) ────────────


@pytest.mark.asyncio
async def test_an_invalid_code_is_recorded_against_the_only_unbound_box():
    hass = _hass(_keystore(_box(), _box(device_id="EC-2", api_key=None)))
    resp = await _me(hass, api_key="unbound", pair_code="ABCDEF")
    assert resp.status == 403
    refusals = edge_api.edge_refusals(hass)
    assert list(refusals) == ["EC-2"]
    assert refusals["EC-2"]["error"] == "pair_code_invalid"


@pytest.mark.asyncio
async def test_an_invalid_code_blames_no_box_when_several_are_unbound():
    ks = _keystore(_box(api_key=None), _box(device_id="EC-2", api_key=None))
    hass = _hass(ks)
    resp = await _me(hass, api_key="unbound", pair_code="ABCDEF")
    assert _json(resp) == {"error": "pair_code_invalid"}
    assert edge_api.edge_refusals(hass) == {}


@pytest.mark.asyncio
async def test_a_bound_key_with_another_box_code_is_recorded_against_it():
    ks = _keystore(_box(), _box(device_id="EC-2", api_key=None))
    code = await ks.async_issue_pair_code("EC-2")
    hass = _hass(ks)
    resp = await _me(hass, api_key=BOX_KEY, pair_code=code)
    assert _json(resp) == {"error": "pair_code_invalid"}
    assert edge_api.edge_refusals(hass)["EC-1"]["error"] == "pair_code_invalid"
    assert "EC-2" not in edge_api.edge_refusals(hass)


@pytest.mark.asyncio
async def test_binding_clears_the_box_refusal():
    ks = _keystore(_box(api_key=None))
    hass = _hass(ks)
    await _me(hass, api_key="new-key", pair_code="ABCDEF")
    assert "EC-1" in edge_api.edge_refusals(hass)
    code = await ks.async_issue_pair_code("EC-1")
    assert (await _me(hass, api_key="new-key", pair_code=code)).status == 200
    assert edge_api.edge_refusals(hass) == {}
