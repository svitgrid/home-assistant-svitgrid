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

import logging
from unittest.mock import MagicMock

import pytest
from multidict import CIMultiDict

from custom_components.svitgrid import edge_api
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
    (edge_api.EdgeTrustedKeysView, "get", {}),
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


@pytest.mark.asyncio
async def test_unknown_key_with_pair_code_is_401_until_task_11():
    """The pairing-code branch is Task 11; until then a code changes nothing."""
    hass = _hass(_keystore(_box()))
    view = edge_api.EdgeMeView()
    request = _FakeRequest(hass, api_key="unbound")
    request.headers["X-Pair-Code"] = "ABCDEF"
    resp = await view.get(request)
    assert resp.status == 401
    assert _json(resp) == {"error": "unauthorized"}


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
    ],
    ids=["zigbee", "sensor-readings"],
)
async def test_zigbee_sinks_answer_empty(view_cls, match_info):
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


@pytest.mark.asyncio
async def test_trusted_keys_lists_the_keystore_keys():
    hass = _hass(_keystore(_box(), trusted={"k1": "04aa", "k2": "04bb"}))
    resp = await _call(edge_api.EdgeTrustedKeysView, "get", hass, api_key=BOX_KEY)
    assert resp.status == 200
    keys = _json(resp)["trustedKeys"]
    assert sorted(keys, key=lambda k: k["keyId"]) == [
        {"keyId": "k1", "publicKeyHex": "04aa"},
        {"keyId": "k2", "publicKeyHex": "04bb"},
    ]


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
        edge_api.EdgeTrustedKeysView: "/api/v3/executors/trusted-keys",
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
