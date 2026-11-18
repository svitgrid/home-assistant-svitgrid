"""The Zigbee sockets a rule switches on an edge island (svitgrid D-2158, D-2159).

A port of what the cloud's evaluator switches (services/api/src/calendar/
evaluator.ts and smart-device-except.ts):

- a custom rule's ``smart_device`` action sets each named socket to
  ``activeState`` while the rule is active. Its release hands back, per
  socket, what another rule that let go of it meanwhile wanted (D-1795), else
  the state the socket had when the rule took it (``socketBaselines``,
  D-1824), else ``restoreState``. A socket another active rule holds is left
  as that rule set it (#1507).
- ``smart_device_except {keepIds}`` switches off every switch but the kept
  ones and those a «Критичні пристрої» ``stage_1`` sibling switches, records
  them in ``switchedOffIds``, and its release switches exactly those back on.
- ``lower_consumption`` switches ``smartDeviceIds`` off while active and on at
  release; ``consume_from_sun`` the other way round.

The planners are pure: the scheduler hands them the socket roster
(``SocketSwitcher.async_roster``) and the inverter's other events, and they
record what the release needs in the event's new execution state.
``SocketSwitcher`` queues each ``set_state`` for the board the socket is
paired with, signed by the rule's stored envelope. The board runs
``set_state`` only signed (firmware ``command_auth.c``), so a rule with no
stored signature switches nothing.

Socket commands are queued, not awaited, as the cloud creates its
``smartDeviceCommands`` and moves on: a socket that never answers does not
hold the inverter's writes back, and the box's ack updates the socket's state
(``edge_api._apply_set_state``).
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

SOCKET_ACTION_TYPES = frozenset({"smart_device", "smart_device_except"})
# The modes that only switch sockets, and the state each sets while active.
SOCKET_MODES = {"lower_consumption": False, "consume_from_sun": True}

# executionState keys (the API's).
BASELINES_KEY = "socketBaselines"
SWITCHED_OFF_KEY = "switchedOffIds"
# What this rule handed back on each socket at its last release. The cloud
# records it on the socket (`smartDevices.ruleRelease`); the island has no
# socket document the scheduler writes, so the rule keeps it.
RELEASES_KEY = "socketReleases"

# SOCKET_HANDOVER_SLACK_MS (evaluator.ts): one evaluation pass, no more (D-1876).
_HANDOVER_SLACK_MS = 10_000
# Bit 0 of a device's capabilities: it switches on and off.
_CAP_ON_OFF = 1

OUTAGE_ESSENTIALS_TEMPLATE_ID = "outage_essentials"

# Why a socket was not switched, as the rule's history names it.
SOCKET_NOT_FOUND = "socket_not_found"
EDGE_DEVICE_MISSING = "edge_device_missing"
UNSIGNED_WRITE = "unsigned_write"


def _actions(event: dict) -> list[dict]:
    actions = (event.get("config") or {}).get("customActions") or []
    return [a for a in actions if isinstance(a, dict)]


def _ids(value) -> list[str]:
    return [i for i in value or () if isinstance(i, str) and i]


def uses_sockets(event: dict) -> bool:
    """Whether *event* switches sockets: a socket mode, or a custom rule with
    a socket action."""
    mode = event.get("mode")
    if mode in SOCKET_MODES:
        return True
    return mode == "custom" and any(a.get("type") in SOCKET_ACTION_TYPES for a in _actions(event))


def _iso_ms(value) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _stage_one_ids(event: dict, others) -> set[str]:
    """stageOneDeviceIds: the sockets the enabled «Критичні пристрої»
    ``stage_1`` members of *event*'s scope switch themselves."""
    if event.get("templateId") != OUTAGE_ESSENTIALS_TEMPLATE_ID:
        return set()
    always = (event.get("schedule") or {}).get("recurrence") == "always"
    out: set[str] = set()
    for other in others or ():
        if other.get("id") == event.get("id") or other.get("enabled") is False:
            continue
        if other.get("templateId") != OUTAGE_ESSENTIALS_TEMPLATE_ID:
            continue
        if other.get("memberKey") != "stage_1":
            continue
        if ((other.get("schedule") or {}).get("recurrence") == "always") != always:
            continue
        for action in _actions(other):
            if action.get("type") == "smart_device":
                out.update(_ids(action.get("deviceIds")))
    return out


def _can_switch(device: dict) -> bool:
    if device.get("sensor"):
        return False
    caps = device.get("caps")
    return caps is None or caps == 0 or bool(caps & _CAP_ON_OFF)


def _except_targets(roster, keep: set[str]) -> list[str]:
    """selectExceptTargets: every switch not kept and not known to be off. A
    socket whose state is unknown is included, and one with an unfinished
    ``on`` counts as on."""
    out = []
    for device in roster or ():
        if not _can_switch(device) or device["id"] in keep:
            continue
        if device.get("on") is False and not device.get("pendingOn"):
            continue
        out.append(device["id"])
    return out


def plan_activation(
    event: dict, exec_state: dict, new_state: dict, roster, others=()
) -> list[tuple[str, bool]]:
    """The ``(socket id, on)`` switches of *event*'s activation, recording in
    *new_state* what its release needs."""
    mode = event.get("mode")
    if mode in SOCKET_MODES:
        on = SOCKET_MODES[mode]
        return [(i, on) for i in _ids((event.get("config") or {}).get("smartDeviceIds"))]
    by_id = {d["id"]: d for d in roster or ()}
    switches: list[tuple[str, bool]] = []
    for action in _actions(event):
        kind = action.get("type")
        if kind == "smart_device" and isinstance(action.get("activeState"), bool):
            ids = _ids(action.get("deviceIds"))
            baselines = dict(new_state.get(BASELINES_KEY) or {})
            for i in ids:
                device = by_id.get(i) or {}
                if device.get("reachable") is not False and isinstance(device.get("on"), bool):
                    baselines[i] = device["on"]
            new_state[BASELINES_KEY] = baselines
            switches += [(i, action["activeState"]) for i in ids]
        elif kind == "smart_device_except":
            keep = set(_ids(action.get("keepIds"))) | _stage_one_ids(event, others)
            selected = _except_targets(roster, keep)
            new_state[SWITCHED_OFF_KEY] = selected
            switches += [(i, False) for i in selected]
    return switches


def _held_by_other_active(event: dict, others) -> set[str]:
    held: set[str] = set()
    for other in others or ():
        if other.get("id") == event.get("id") or other.get("mode") != "custom":
            continue
        if (other.get("executionState") or {}).get("status") != "active":
            continue
        for action in _actions(other):
            if action.get("type") == "smart_device":
                held.update(_ids(action.get("deviceIds")))
    return held


def _released_by_other(event: dict, exec_state: dict, others, device_id: str) -> bool | None:
    """What another rule handed back on *device_id* after this one took it."""
    taken_ms = _iso_ms(exec_state.get("lastActivatedAt"))
    if taken_ms is None:
        return None
    latest = None
    for other in others or ():
        if other.get("id") == event.get("id"):
            continue
        release = ((other.get("executionState") or {}).get(RELEASES_KEY) or {}).get(device_id)
        if not isinstance(release, dict) or not isinstance(release.get("on"), bool):
            continue
        at = release.get("atMs")
        if not isinstance(at, int | float) or at < taken_ms - _HANDOVER_SLACK_MS:
            continue
        if latest is None or at > latest[0]:
            latest = (at, release["on"])
    return None if latest is None else latest[1]


def plan_release(
    event: dict, exec_state: dict, new_state: dict, others, now_ms: int
) -> list[tuple[str, bool]]:
    """The ``(socket id, on)`` switches of *event*'s release, dropping from
    *new_state* what its activation recorded."""
    mode = event.get("mode")
    if mode in SOCKET_MODES:
        on = not SOCKET_MODES[mode]
        return [(i, on) for i in _ids((event.get("config") or {}).get("smartDeviceIds"))]
    switches: list[tuple[str, bool]] = []
    held = None
    releases = dict(new_state.get(RELEASES_KEY) or {})
    baselines = exec_state.get(BASELINES_KEY) or {}
    for action in reversed(_actions(event)):
        kind = action.get("type")
        if kind == "smart_device" and isinstance(action.get("restoreState"), bool):
            if held is None:
                held = _held_by_other_active(event, others)
            for i in _ids(action.get("deviceIds")):
                baseline = baselines.get(i)
                on = baseline if isinstance(baseline, bool) else action["restoreState"]
                other = _released_by_other(event, exec_state, others, i)
                if other is not None:
                    on = other
                releases[i] = {"on": on, "atMs": now_ms}
                if i not in held:
                    switches.append((i, on))
        elif kind == "smart_device_except":
            recorded = exec_state.get(SWITCHED_OFF_KEY)
            switches += [(i, True) for i in dict.fromkeys(_ids(recorded))]
    new_state.pop(BASELINES_KEY, None)
    new_state.pop(SWITCHED_OFF_KEY, None)
    if releases:
        new_state[RELEASES_KEY] = releases
    # The cloud groups a release's commands, off first; the order of the
    # actions within each group is kept.
    return sorted(switches, key=lambda s: s[1]) if switches else switches


def resolve_switches(switches, roster) -> tuple[list[tuple[dict, bool]], list[tuple[str, str]]]:
    """Split *switches* into ``(device, on)`` sends and ``(id, reason)``
    skips: a socket the island does not know, or one whose board is not
    adopted, is skipped. A socket named twice is sent once, its last state."""
    by_id = {d["id"]: d for d in roster or ()}
    last = dict(switches)
    sends: list[tuple[dict, bool]] = []
    skipped: list[tuple[str, str]] = []
    for device_id in dict.fromkeys(i for i, _ in switches):
        device = by_id.get(device_id)
        if device is None:
            skipped.append((device_id, SOCKET_NOT_FOUND))
        elif not device.get("boxAdopted"):
            skipped.append((device_id, EDGE_DEVICE_MISSING))
        else:
            sends.append((device, last[device_id]))
    return sends, skipped


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


class SocketSwitcher:
    """Reads the island's sockets and queues a rule's ``set_state`` for them.

    *devices* is the ZigbeeDeviceStore, *queue* the EdgeCommandQueue and
    *boxes* a callable, sync or async, returning the ids of the adopted boxes.
    """

    def __init__(self, devices, queue, boxes) -> None:
        self._devices = devices
        self._queue = queue
        self._boxes = boxes

    async def _box_ids(self) -> set[str]:
        return set(await _maybe_await(self._boxes()) or ())

    async def async_available(self) -> bool:
        """True once a box is adopted: only a box has a Zigbee hub."""
        return bool(await self._box_ids())

    async def async_roster(self) -> list[dict]:
        """Every Zigbee device the island knows, as the planners read it."""
        boxes = await self._box_ids()
        devices = await self._devices.async_list()
        pending_on: set[str] = set()
        for box_id in {d["deviceId"] for d in devices} & boxes:
            for row in await self._queue.async_pending(box_id, "set_state"):
                if (row.get("payload") or {}).get("on") is True:
                    device_id = (row.get("target") or {}).get("smartDeviceId")
                    if device_id:
                        pending_on.add(device_id)
        roster = []
        for device in devices:
            _socket, sensor = await self._devices.async_latest(device["id"])
            roster.append(
                {
                    "id": device["id"],
                    "deviceId": device["deviceId"],
                    "addr": device["addr"],
                    "endpoint": device["endpoint"],
                    "on": device.get("on"),
                    "reachable": device.get("reachable"),
                    "caps": device.get("caps"),
                    "sensor": sensor is not None,
                    "pendingOn": device["id"] in pending_on,
                    "boxAdopted": device["deviceId"] in boxes,
                }
            )
        return roster

    async def async_switch(self, event: dict, sends) -> list[tuple[str, str]]:
        """Queue ``set_state`` for each ``(device, on)`` in *sends*, signed by
        *event*'s stored envelope. Returns the ``(id, reason)`` of each socket
        not switched."""
        signing = event.get("signing")
        if not isinstance(signing, dict) or not signing.get("signature"):
            if sends:
                _LOGGER.warning(
                    "Rule %s carries no phone signature; its sockets are not switched",
                    event.get("id"),
                )
            return [(device["id"], UNSIGNED_WRITE) for device, _ in sends]
        skipped: list[tuple[str, str]] = []
        for device, on in sends:
            try:
                command_id = await self._queue.async_enqueue(
                    device["deviceId"],
                    "set_state",
                    {"on": on},
                    signing=signing,
                    inverter_id=event.get("inverterId"),
                    source="rule",
                    target={
                        "smartDeviceId": device["id"],
                        "zigbeeAddr": device["addr"],
                        "zigbeeEndpoint": device["endpoint"],
                    },
                )
                await self._devices.async_add_history(
                    device["id"], "command_sent", {"on": on}, command_id
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Rule %s: could not queue set_state for %s", event.get("id"), device["id"]
                )
                skipped.append((device["id"], "queue_failed"))
        return skipped


# ── «Критичні пристрої»: a today setting overrides the standing one ─────────


def _member_of_template(event: dict) -> bool:
    return (
        event.get("templateId") == OUTAGE_ESSENTIALS_TEMPLATE_ID and event.get("mode") == "custom"
    )


def _recurrence(event: dict):
    return (event.get("schedule") or {}).get("recurrence")


def _active_member(event: dict, others, recurrence: str) -> dict | None:
    """activeTemplateMember: an enabled, active member of *event*'s template
    with *recurrence*."""
    for other in others or ():
        if other.get("id") == event.get("id") or other.get("enabled") is False:
            continue
        if not _member_of_template(other) or _recurrence(other) != recurrence:
            continue
        if (other.get("executionState") or {}).get("status") == "active":
            return other
    return None


def template_gate(event: dict, others, now_utc: datetime, tz: str) -> tuple[str, str] | None:
    """What the «Критичні пристрої» hand-over asks of *event* this tick, as
    the cloud's evaluateSingleEvent gates it, or None.

    - ``("stand_down", id)``: a standing (``always``) member while an
      enabled today member is in its window (templateSiblingInWindow). It
      releases if active and stays idle until that window ends.
    - ``("wait", id)``: a member that would activate while the other side's
      member is still active. Its release has not been queued yet, so the
      sockets it is about to switch on would read as off and be left on.
    """
    if not _member_of_template(event):
        return None
    # The evaluator's window test (event_evaluator imports nothing from here).
    from .event_evaluator import is_event_in_window

    active = (event.get("executionState") or {}).get("status") == "active"
    recurrence = _recurrence(event)
    if recurrence == "always":
        for other in others or ():
            if other.get("id") == event.get("id") or other.get("enabled") is False:
                continue
            if other.get("templateId") != OUTAGE_ESSENTIALS_TEMPLATE_ID:
                continue
            if _recurrence(other) != "none":
                continue
            if is_event_in_window(other.get("schedule") or {}, now_utc, tz):
                return ("stand_down", other.get("id"))
        if not active:
            holder = _active_member(event, others, "none")
            if holder is not None:
                return ("wait", holder.get("id"))
        return None
    if recurrence == "none" and not active:
        if not is_event_in_window(event.get("schedule") or {}, now_utc, tz):
            return None
        holder = _active_member(event, others, "always")
        if holder is not None:
            return ("wait", holder.get("id"))
    return None
