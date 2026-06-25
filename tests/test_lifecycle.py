from custom_components.svitgrid.lifecycle import LifecycleState, ACTIVE, PAUSED, DEPROVISIONED


def test_starts_active():
    lc = LifecycleState()
    assert lc.state == ACTIVE and lc.active is True


def test_deprovision_sets_state_reason_since():
    lc = LifecycleState()
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    assert lc.state == DEPROVISIONED and lc.reason == "revoked"
    assert lc.since == "2026-06-25T10:00:00Z" and lc.active is False


def test_idempotent_keeps_first_since():
    lc = LifecycleState()
    lc.pause("disabled", "2026-06-25T10:00:00Z")
    lc.pause("disabled", "2026-06-25T11:00:00Z")  # same → no-op
    assert lc.since == "2026-06-25T10:00:00Z"


def test_mirrors_to_activity():
    class _Act:
        def __init__(self): self.calls = []
        def set_lifecycle(self, state, reason): self.calls.append((state, reason))
    a = _Act()
    lc = LifecycleState(activity=a)
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    assert a.calls == [("deprovisioned", "revoked")]
