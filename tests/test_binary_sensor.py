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
