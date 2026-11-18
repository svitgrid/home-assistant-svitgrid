"""Island Telegram liveness: a board or an inverter that stops reporting (svitgrid D-2149)."""

import json

from custom_components.svitgrid.telegram.liveness import evaluate_liveness

MIN = 60_000
T0 = 1_793_181_600_000


def _hh(*, inverters=(), boards=(), grid_out=False, household="hh1"):
    return {
        "householdId": household,
        "gridOut": grid_out,
        "inverters": [dict(i) for i in inverters],
        "boards": [dict(b) for b in boards],
    }


def _run(snapshots, *, started=T0, state=None):
    """Feed (now_ms, snapshot) pairs; return [(index, kind, subject, minutesAgo)]."""
    state = state or {}
    got = []
    for index, (now, snapshot) in enumerate(snapshots):
        state, alerts = evaluate_liveness(state, snapshot, now, started)
        state = json.loads(json.dumps(state))
        got += [(index, a.kind, a.subject_id, a.minutes_ago) for a in alerts]
    return state, got


def _inv(last, board=None, inv="inv1"):
    return {"id": inv, "lastReadingMs": last, "boardId": board}


def _board(contact, *, paused=False, inverters=("inv-b",), device="EC-1"):
    return {
        "deviceId": device,
        "lastContactMs": contact,
        "paused": paused,
        "inverterIds": list(inverters),
    }


def test_inverter_silent_15_minutes_sends_offline_once():
    last = T0 + MIN
    _, got = _run(
        [
            (T0 + 15 * MIN, [_hh(inverters=[_inv(last)])]),
            (T0 + 16 * MIN, [_hh(inverters=[_inv(last)])]),
            (T0 + 40 * MIN, [_hh(inverters=[_inv(last)])]),
        ]
    )
    assert got == [(1, "offline", "inv1", 15)]


def test_offline_reminds_after_24_hours_and_clears_on_a_reading():
    last = T0
    day = 24 * 60 * MIN
    state, got = _run(
        [
            (T0 + 15 * MIN, [_hh(inverters=[_inv(last)])]),
            (T0 + 15 * MIN + day - MIN, [_hh(inverters=[_inv(last)])]),
            (T0 + 15 * MIN + day, [_hh(inverters=[_inv(last)])]),
        ]
    )
    assert [(i, k) for i, k, _, _ in got] == [(0, "offline"), (2, "offline")]
    # A reading ends the episode: the next silence is a new one, sent at once.
    back = T0 + 2 * day
    state, got = _run([(back + MIN, [_hh(inverters=[_inv(back)])])], state=state)
    assert got == []
    _, got = _run([(back + 16 * MIN, [_hh(inverters=[_inv(back)])])], state=state)
    assert [(k, m) for _, k, _, m in got] == [("offline", 16)]


def test_ha_downtime_does_not_count():
    """A reading from before HA started is stale because HA was down, not the
    inverter: the 15 minutes run from the start."""
    started = T0 + 60 * MIN
    _, got = _run(
        [
            (started + 14 * MIN, [_hh(inverters=[_inv(T0)])]),
            (started + 15 * MIN, [_hh(inverters=[_inv(T0)])]),
        ],
        started=started,
    )
    assert got == [(1, "offline", "inv1", 75)]


def test_never_reported_inverter_sends_nothing():
    _, got = _run([(T0 + 60 * MIN, [_hh(inverters=[_inv(None)])])])
    assert got == []


def test_silent_board_sends_offline_board_and_recovers():
    contact = T0 + MIN
    inv = _inv(T0 + MIN, board="EC-1", inv="inv-b")
    _, got = _run(
        [
            (T0 + 15 * MIN, [_hh(inverters=[inv], boards=[_board(contact)])]),
            (T0 + 16 * MIN, [_hh(inverters=[inv], boards=[_board(contact)])]),
            (T0 + 30 * MIN, [_hh(inverters=[inv], boards=[_board(contact)])]),
            (T0 + 31 * MIN, [_hh(inverters=[inv], boards=[_board(T0 + 31 * MIN)])]),
            (T0 + 32 * MIN, [_hh(inverters=[inv], boards=[_board(T0 + 32 * MIN)])]),
        ]
    )
    # The board's inverter is silent too, but the board message covers it.
    assert got == [
        (1, "offline_board", "EC-1", 15),
        (3, "offline_board_recovered", "EC-1", None),
    ]


def test_silent_board_in_an_outage_says_so():
    snap = [_hh(boards=[_board(T0)], grid_out=True)]
    _, got = _run([(T0 + 15 * MIN, snap)])
    assert got == [(0, "offline_outage", "EC-1", 15)]


def test_paused_board_sends_nothing():
    """A paused board uploads nothing by design. It keeps polling, so its
    contact stays fresh, and its inverters' silence is not an outage."""
    inv = _inv(T0, board="EC-1", inv="inv-b")
    _, got = _run(
        [
            (T0 + 30 * MIN, [_hh(inverters=[inv], boards=[_board(T0 + 29 * MIN, paused=True)])]),
            (T0 + 60 * MIN, [_hh(inverters=[inv], boards=[_board(T0, paused=True)])]),
        ]
    )
    assert got == []


def test_board_reporting_but_inverter_silent_sends_offline():
    inv = _inv(T0, board="EC-1", inv="inv-b")
    _, got = _run([(T0 + 20 * MIN, [_hh(inverters=[inv], boards=[_board(T0 + 19 * MIN)])])])
    assert got == [(0, "offline", "inv-b", 20)]


def test_board_that_never_reported_sends_nothing():
    _, got = _run([(T0 + 60 * MIN, [_hh(boards=[_board(None)])])])
    assert got == []


def test_gone_inverter_or_household_is_forgotten():
    state, got = _run([(T0 + 15 * MIN, [_hh(inverters=[_inv(T0)])])])
    assert got[0][1] == "offline"
    state, _ = evaluate_liveness(state, [], T0 + 16 * MIN, T0)
    assert state == {"households": {}}
