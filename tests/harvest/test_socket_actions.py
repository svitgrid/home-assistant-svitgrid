"""The sockets a rule switches on an edge island (svitgrid D-2158, D-2159).

The cloud's evaluator switches Zigbee sockets for a custom rule's
`smart_device` and `smart_device_except` actions, and for the
`lower_consumption` and `consume_from_sun` modes (evaluator.ts
`activateCustom`, `deactivateCustom`, `smart-device-except.ts`,
`evaluateLowerConsumption`, `evaluateConsumeFromSun`). These tests pin the
island's port of what it switches, and what it records for the release.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.svitgrid.harvest.socket_actions import (
    SocketSwitcher,
    plan_activation,
    plan_release,
    resolve_switches,
    uses_sockets,
)

_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _socket(device_id, on=True, *, box="EC-1", reachable=True, caps=None, sensor=False):
    return {
        "id": device_id,
        "deviceId": box,
        "addr": f"0x{device_id[-4:]}",
        "endpoint": 1,
        "on": on,
        "reachable": reachable,
        "caps": caps,
        "sensor": sensor,
        "pendingOn": False,
        "boxAdopted": True,
    }


def _rule(actions, *, event_id="r-1", status="idle", **state):
    return {
        "id": event_id,
        "inverterId": "inv-1",
        "enabled": True,
        "mode": "custom",
        "schedule": {"recurrence": "always"},
        "config": {"customActions": actions},
        "executionState": {"status": status, **state},
    }


def _plug(ids, active=False, restore=True):
    return {
        "type": "smart_device",
        "deviceIds": ids,
        "activeState": active,
        "restoreState": restore,
    }


# ── which events switch sockets ──────────────────────────────────────────────


def test_uses_sockets_names_the_socket_actions_and_modes():
    assert uses_sockets(_rule([_plug(["a"])]))
    assert uses_sockets(_rule([{"type": "smart_device_except", "keepIds": []}]))
    assert uses_sockets({"mode": "lower_consumption", "config": {"smartDeviceIds": ["a"]}})
    assert uses_sockets({"mode": "consume_from_sun", "config": {"smartDeviceIds": ["a"]}})
    assert not uses_sockets(_rule([{"type": "work_mode", "value": 1}]))
    assert not uses_sockets({"mode": "use_battery", "config": {}})


# ── smart_device ──────────────────────────────────────────────────────────────


def test_smart_device_switches_each_socket_to_its_active_state():
    rule = _rule([_plug(["a", "b"], active=False)])
    state: dict = {}
    switches = plan_activation(rule, rule["executionState"], state, [_socket("a"), _socket("b")])
    assert switches == [("a", False), ("b", False)]


def test_smart_device_records_the_state_each_reachable_socket_had():
    """socketBaselines (D-1824): a socket whose state is unknown, or that its
    hub marks unreachable, is left out."""
    rule = _rule([_plug(["a", "b", "c", "d"])])
    roster = [_socket("a", on=True), _socket("b", on=False), _socket("c", on=None)]
    roster.append(_socket("d", on=True, reachable=False))
    state: dict = {}
    plan_activation(rule, rule["executionState"], state, roster)
    assert state["socketBaselines"] == {"a": True, "b": False}


def test_smart_device_release_hands_back_the_baseline_then_the_restore_state():
    rule = _rule(
        [_plug(["a", "b"], active=False, restore=True)],
        status="active",
        lastActivatedAt="2026-11-18T11:00:00+00:00",
        socketBaselines={"a": False},
    )
    state = dict(rule["executionState"])
    switches = plan_release(rule, rule["executionState"], state, [], _NOW_MS)
    # a was off when the rule took it; b's state was unknown, so restoreState.
    assert switches == [("a", False), ("b", True)]
    assert "socketBaselines" not in state
    assert state["socketReleases"] == {
        "a": {"on": False, "atMs": _NOW_MS},
        "b": {"on": True, "atMs": _NOW_MS},
    }


def test_smart_device_release_leaves_a_socket_another_active_rule_holds():
    """#1507: restoring it would undo the other rule's state."""
    rule = _rule([_plug(["a", "b"])], status="active")
    other = _rule([_plug(["b"], active=True)], event_id="r-2", status="active")
    state = dict(rule["executionState"])
    switches = plan_release(rule, rule["executionState"], state, [other], _NOW_MS)
    assert switches == [("a", True)]


def test_a_pending_rule_does_not_hold_a_socket():
    rule = _rule([_plug(["a"])], status="active")
    other = _rule([_plug(["a"], active=True)], event_id="r-2", status="pending_condition")
    state = dict(rule["executionState"])
    assert plan_release(rule, rule["executionState"], state, [other], _NOW_MS) == [("a", True)]


def test_a_release_by_another_rule_while_this_one_held_the_socket_wins():
    """D-1795: the other rule let go of the socket after this one took it, so
    its hand-back is what the socket returns to."""
    taken = "2026-11-18T11:00:00+00:00"
    taken_ms = int(datetime.fromisoformat(taken).timestamp() * 1000)
    rule = _rule(
        [_plug(["a"], active=False, restore=True)],
        status="active",
        lastActivatedAt=taken,
        socketBaselines={"a": True},
    )
    later = _rule(
        [_plug(["a"])],
        event_id="r-2",
        socketReleases={"a": {"on": False, "atMs": taken_ms + 60_000}},
    )
    stale = _rule(
        [_plug(["a"])],
        event_id="r-3",
        socketReleases={"a": {"on": True, "atMs": taken_ms - 60_000}},
    )
    state = dict(rule["executionState"])
    switches = plan_release(rule, rule["executionState"], state, [later, stale], _NOW_MS)
    assert switches == [("a", False)]


# ── smart_device_except ──────────────────────────────────────────────────────


def test_except_switches_off_every_switch_but_the_kept_ones():
    """selectExceptTargets: a sensor, a device that cannot switch and a socket
    known to be off are left alone; one whose state is unknown is switched."""
    rule = _rule([{"type": "smart_device_except", "keepIds": ["fridge"]}])
    roster = [
        _socket("fridge"),
        _socket("boiler"),
        _socket("unknown", on=None),
        _socket("off", on=False),
        _socket("thermo", sensor=True),
        _socket("meter", caps=2),
        _socket("plug", caps=3),
    ]
    state: dict = {}
    switches = plan_activation(rule, rule["executionState"], state, roster)
    assert switches == [("boiler", False), ("unknown", False), ("plug", False)]
    assert state["switchedOffIds"] == ["boiler", "unknown", "plug"]


def test_except_counts_a_socket_with_an_unfinished_on_as_on():
    rule = _rule([{"type": "smart_device_except", "keepIds": []}])
    off_but_coming_on = {**_socket("boiler", on=False), "pendingOn": True}
    state: dict = {}
    assert plan_activation(rule, rule["executionState"], state, [off_but_coming_on]) == [
        ("boiler", False)
    ]


def test_except_leaves_out_the_sockets_a_stage_1_sibling_switches():
    """stageOneDeviceIds: «Критичні пристрої» `stage_1` switches these itself."""
    final = {
        **_rule([{"type": "smart_device_except", "keepIds": []}]),
        "templateId": "outage_essentials",
        "memberKey": "final",
    }
    stage_1 = {
        **_rule([_plug(["tv"])], event_id="r-2"),
        "templateId": "outage_essentials",
        "memberKey": "stage_1",
    }
    other_scope = {
        **stage_1,
        "id": "r-3",
        "schedule": {"recurrence": "none", "startTime": "10:00", "endTime": "23:59"},
        "config": {"customActions": [_plug(["kettle"])]},
    }
    roster = [_socket("tv"), _socket("kettle"), _socket("boiler")]
    state: dict = {}
    switches = plan_activation(
        final, final["executionState"], state, roster, [stage_1, other_scope]
    )
    assert switches == [("kettle", False), ("boiler", False)]


def test_except_release_switches_back_on_exactly_what_it_switched_off():
    rule = _rule(
        [{"type": "smart_device_except", "keepIds": []}],
        status="active",
        switchedOffIds=["boiler", "boiler", "tv"],
    )
    state = dict(rule["executionState"])
    switches = plan_release(rule, rule["executionState"], state, [], _NOW_MS)
    assert switches == [("boiler", True), ("tv", True)]
    assert "switchedOffIds" not in state


# ── the two modes (D-2159) ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "on_activate"), [("lower_consumption", False), ("consume_from_sun", True)]
)
def test_modes_switch_their_sockets_and_back(mode, on_activate):
    event = {
        "id": "e-1",
        "inverterId": "inv-1",
        "mode": mode,
        "config": {"smartDeviceIds": ["a", "b"]},
        "executionState": {"status": "idle"},
    }
    state: dict = {}
    assert plan_activation(event, event["executionState"], state, []) == [
        ("a", on_activate),
        ("b", on_activate),
    ]
    active = {"status": "active"}
    assert plan_release(event, active, dict(active), [], _NOW_MS) == [
        ("a", not on_activate),
        ("b", not on_activate),
    ]


# ── which sockets can be reached ─────────────────────────────────────────────


def test_a_socket_on_no_adopted_board_is_skipped_with_a_reason():
    roster = [_socket("a"), {**_socket("b"), "boxAdopted": False}]
    sends, skipped = resolve_switches([("a", False), ("b", False), ("gone", False)], roster)
    assert [(d["id"], on) for d, on in sends] == [("a", False)]
    assert skipped == [("b", "edge_device_missing"), ("gone", "socket_not_found")]


# ── the switcher: the signed set_state on each socket's board ───────────────


class _Queue:
    def __init__(self, pending=None):
        self.enqueued: list = []
        self._pending = pending or {}

    async def async_enqueue(self, device_id, command, payload, **kwargs):
        self.enqueued.append((device_id, command, payload, kwargs))
        return f"cmd-{len(self.enqueued)}"

    async def async_pending(self, device_id, command):
        return self._pending.get(device_id, [])


class _Devices:
    def __init__(self, devices, sensors=()):
        self._devices = devices
        self._sensors = set(sensors)
        self.history: list = []

    async def async_list(self, box_id=None):
        return [d for d in self._devices if box_id is None or d["deviceId"] == box_id]

    async def async_latest(self, device_id):
        return None, ({"captured_at": "x"} if device_id in self._sensors else None)

    async def async_add_history(self, device_id, event, state, command_id):
        self.history.append((device_id, event, state, command_id))


def _stored(device_id, box="EC-1", on=True):
    return {
        "id": device_id,
        "deviceId": box,
        "addr": "0x1a2b",
        "endpoint": 1,
        "on": on,
        "reachable": True,
        "caps": None,
    }


_SIGNING = {
    "signedEventData": {"scenarioId": "r-1", "authorizedCommands": ["set_state"]},
    "signature": "MEUCIQ",
    "signingKeyId": "phone-1",
}


@pytest.mark.asyncio
async def test_switcher_queues_set_state_signed_by_the_rules_envelope():
    queue = _Queue()
    devices = _Devices([_stored("a", box="EC-2")])
    switcher = SocketSwitcher(devices, queue, lambda: {"EC-1", "EC-2"})
    roster = await switcher.async_roster()
    event = {**_rule([_plug(["a"])]), "signing": _SIGNING}

    skipped = await switcher.async_switch(event, [(roster[0], False)])

    assert skipped == []
    assert queue.enqueued == [
        (
            "EC-2",
            "set_state",
            {"on": False},
            {
                "signing": _SIGNING,
                "inverter_id": "inv-1",
                "source": "rule",
                "target": {"smartDeviceId": "a", "zigbeeAddr": "0x1a2b", "zigbeeEndpoint": 1},
            },
        )
    ]
    assert devices.history == [("a", "command_sent", {"on": False}, "cmd-1")]


@pytest.mark.asyncio
async def test_switcher_never_queues_an_unsigned_set_state():
    """The board refuses an unsigned set_state (command_auth.c), so a rule
    stored without the phone's signature skips every socket."""
    queue = _Queue()
    switcher = SocketSwitcher(_Devices([_stored("a")]), queue, lambda: {"EC-1"})
    roster = await switcher.async_roster()

    skipped = await switcher.async_switch(_rule([_plug(["a"])]), [(roster[0], False)])

    assert queue.enqueued == []
    assert skipped == [("a", "unsigned_write")]


@pytest.mark.asyncio
async def test_switcher_roster_marks_sensors_boards_and_unfinished_ons():
    queue = _Queue(
        pending={
            "EC-1": [
                {"payload": {"on": True}, "target": {"smartDeviceId": "b"}},
                {"payload": {"on": False}, "target": {"smartDeviceId": "a"}},
            ]
        }
    )
    devices = _Devices(
        [_stored("a"), _stored("b", on=False), _stored("t"), _stored("x", box="EC-9")],
        sensors={"t"},
    )
    switcher = SocketSwitcher(devices, queue, lambda: {"EC-1"})

    roster = {d["id"]: d for d in await switcher.async_roster()}

    assert [roster[i]["sensor"] for i in "abt"] == [False, False, True]
    assert [roster[i]["pendingOn"] for i in "ab"] == [False, True]
    assert roster["x"]["boxAdopted"] is False
    assert await switcher.async_available()


@pytest.mark.asyncio
async def test_switcher_is_unavailable_without_an_adopted_board():
    switcher = SocketSwitcher(_Devices([]), _Queue(), lambda: set())
    assert not await switcher.async_available()


# ── «Критичні пристрої»: today overrides always (svitgrid D-2160) ─────────────


def _member(event_id, scope, *, status="idle", enabled=True, start="10:00"):
    schedule = (
        {"recurrence": "always"}
        if scope == "always"
        else {
            "recurrence": "none",
            "startDate": "2026-11-18",
            "startTime": start,
            "endTime": "23:59",
        }
    )
    return {
        "id": event_id,
        "inverterId": "inv-1",
        "enabled": enabled,
        "mode": "custom",
        "templateId": "outage_essentials",
        "memberKey": "final",
        "schedule": schedule,
        "config": {"customActions": [{"type": "smart_device_except", "keepIds": []}]},
        "executionState": {"status": status},
    }


def test_a_today_member_in_its_window_stands_the_always_member_down():
    """templateSiblingInWindow."""
    from custom_components.svitgrid.harvest.socket_actions import template_gate

    always = _member("a", "always", status="active")
    today = _member("t", "today")
    assert template_gate(always, [today], _NOW, "UTC") == ("stand_down", "t")
    later = _member("t", "today", start="13:00")
    assert template_gate(always, [later], _NOW, "UTC") is None
    paused = _member("t", "today", enabled=False)
    assert template_gate(always, [paused], _NOW, "UTC") is None


def test_each_side_of_the_hand_over_waits_for_the_other_to_let_go():
    """activeTemplateMember: an always member waits for an active today
    member whose window is over, and a today member does not activate while
    an always member is still active."""
    from custom_components.svitgrid.harvest.socket_actions import template_gate

    ended_today = _member("t", "today", status="active", start="09:00")
    ended_today["schedule"]["endTime"] = "11:00"
    assert template_gate(_member("a", "always"), [ended_today], _NOW, "UTC") == ("wait", "t")
    active_always = _member("a", "always", status="active")
    assert template_gate(_member("t", "today"), [active_always], _NOW, "UTC") == ("wait", "a")
    # An active today member is left alone.
    today_active = _member("t", "today", status="active")
    assert template_gate(today_active, [active_always], _NOW, "UTC") is None


def test_the_gate_leaves_every_other_event_alone():
    from custom_components.svitgrid.harvest.socket_actions import template_gate

    rule = _rule([_plug(["a"])])
    assert template_gate(rule, [_member("t", "today")], _NOW, "UTC") is None
