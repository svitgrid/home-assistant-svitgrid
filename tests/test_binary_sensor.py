"""Tests for SvitgridProblemBinarySensor."""

from custom_components.svitgrid.activity import ActivityTracker
from custom_components.svitgrid.binary_sensor import SvitgridProblemBinarySensor


def test_problem_on_when_not_active():
    a = ActivityTracker()
    s = SvitgridProblemBinarySensor(a, "entry1", "inv-1", "Deye SG04LP3")
    assert s.is_on is False
    a.set_lifecycle("deprovisioned", "revoked")
    assert s.is_on is True
    assert s.extra_state_attributes["lifecycle_state"] == "deprovisioned"
    assert s.extra_state_attributes["reason"] == "revoked"


def test_problem_on_when_paused():
    """Paused state also reports a problem (spec: deprovisioned AND paused both trigger)."""
    a = ActivityTracker()
    s = SvitgridProblemBinarySensor(a, "entry1", "inv-2", "Deye SG04LP3")
    a.set_lifecycle("paused", "disabled")
    assert s.is_on is True
    assert s.extra_state_attributes["lifecycle_state"] == "paused"


def test_reason_none_for_fresh_tracker():
    """Fresh tracker has no reason and is_on is False."""
    a = ActivityTracker()
    s = SvitgridProblemBinarySensor(a, "entry1", "inv-3", "Deye SG04LP3")
    assert s.is_on is False
    assert s.extra_state_attributes["reason"] is None
