"""Tests for live_gate: the Live send rule and the cadence-message parser.

Both port the edge firmware (live_gate.c, live_cadence.c) rule for rule, so a
Home Assistant install and an edge connector send on the same moves.
"""

from __future__ import annotations

from custom_components.svitgrid.live_gate import (
    LiveControl,
    LiveGate,
    LiveParams,
    parse_live_control,
)

P = LiveParams()


def reading(pv=1000, grid=200, load=800, batt=-400, soc=55.0):
    r = {"pvPower": pv, "gridPower": grid, "loadPower": load, "batteryPower": batt}
    if soc is not None:
        r["batterySoc"] = soc
    return r


def sent_gate(r, now=0.0) -> LiveGate:
    g = LiveGate()
    assert g.should_send(r, now, P)
    g.record_attempt(r, now, ok=True)
    return g


def test_first_reading_sends():
    assert LiveGate().should_send(reading(), 0.0, P)


def test_small_moves_do_not_send():
    g = sent_gate(reading())
    r = reading(pv=1049, grid=151, load=849, batt=-449, soc=55.9)
    assert not g.should_send(r, 10.0, P)


def test_battery_power_plus_50_sends():
    g = sent_gate(reading())
    assert g.should_send(reading(batt=-350), 10.0, P)


def test_soc_plus_one_sends():
    g = sent_gate(reading())
    assert g.should_send(reading(soc=56.0), 10.0, P)


def test_heartbeat_sends_unchanged_reading():
    g = sent_gate(reading())
    assert not g.should_send(reading(), 29.0, P)
    assert g.should_send(reading(), 30.0, P)


def test_missing_keys_are_not_compared():
    g = sent_gate(reading())
    r = reading(soc=None)
    del r["gridPower"]
    assert not g.should_send(r, 10.0, P)
    # And the other way: the last sent reading lacked them.
    g2 = sent_gate(r)
    assert not g2.should_send(reading(grid=5000, soc=90.0), 10.0, P)


def test_failed_attempt_holds_until_heartbeat():
    g = sent_gate(reading())
    g.record_attempt(reading(), 5.0, ok=False)
    assert not g.should_send(reading(pv=1500), 6.0, P)
    assert not g.should_send(reading(pv=1500), 34.0, P)
    assert g.should_send(reading(pv=1500), 35.0, P)


def test_failing_first_send_retries_once_per_heartbeat():
    g = LiveGate()
    g.record_attempt(reading(), 0.0, ok=False)
    assert not g.should_send(reading(), 10.0, P)
    assert g.should_send(reading(), 30.0, P)


def test_deltas_compare_with_last_sent_not_last_seen():
    g = sent_gate(reading(pv=1000))
    assert not g.should_send(reading(pv=1030), 5.0, P)
    assert g.should_send(reading(pv=1060), 10.0, P)


def test_reset_forgets_the_last_send():
    g = sent_gate(reading())
    g.reset()
    assert g.should_send(reading(), 1.0, P)


def test_parse_live_with_params():
    c = parse_live_control(
        {
            "ingestMode": "live",
            "live": {"checkSec": 5, "deltaW": 50, "deltaSocPct": 1, "heartbeatSec": 30},
            "ingestIntervalMs": 30000,
        },
        LiveControl(),
    )
    assert c == LiveControl(mode="live", params=LiveParams(5, 50, 1, 30), interval_s=30)


def test_parse_interval_after_live_turns_live_off():
    live = LiveControl(mode="live", params=LiveParams(check_s=2), interval_s=30)
    c = parse_live_control({"ingestIntervalMs": 300000}, live)
    assert c == LiveControl(mode="interval", params=LiveParams(), interval_s=300)


def test_parse_empty_message_changes_nothing():
    live = LiveControl(mode="live", params=LiveParams(check_s=2), interval_s=30)
    assert parse_live_control({}, live) is live


def test_parse_out_of_range_field_falls_back_to_default():
    c = parse_live_control({"ingestMode": "live", "live": {"deltaW": 0}}, LiveControl())
    assert c.mode == "live"
    assert c.params.delta_w == 50


def test_parse_bad_interval_alone_changes_nothing():
    live = LiveControl(mode="live", interval_s=30)
    assert parse_live_control({"ingestIntervalMs": 0}, live) is live
    assert parse_live_control({"ingestIntervalMs": "30000"}, live) is live
