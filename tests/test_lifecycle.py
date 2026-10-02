from custom_components.svitgrid.lifecycle import ACTIVE, DEPROVISIONED, LifecycleState


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
        def __init__(self):
            self.calls = []

        def set_lifecycle(self, state, reason):
            self.calls.append((state, reason))

    a = _Act()
    lc = LifecycleState(activity=a)
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    assert a.calls == [("deprovisioned", "revoked")]


def test_deprovisioned_is_terminal_pause_cannot_override():
    """C4: once deprovisioned, calling pause() must not change the state."""
    lc = LifecycleState()
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    lc.pause("disabled", "2026-06-25T11:00:00Z")
    assert lc.state == DEPROVISIONED
    assert lc.reason == "revoked"


def test_seed_non_active_mirrors_to_activity():
    """C1: constructing LifecycleState with a non-active seeded state must
    mirror immediately into the activity tracker via __post_init__."""

    class _Act:
        def __init__(self):
            self.calls = []

        def set_lifecycle(self, state, reason):
            self.calls.append((state, reason))

    a = _Act()
    LifecycleState(state=DEPROVISIONED, reason="revoked", activity=a)
    # __post_init__ must have fired the mirror without needing _set()
    assert a.calls == [("deprovisioned", "revoked")]


def test_seed_active_does_not_call_activity():
    """C1: default active construction must NOT call set_lifecycle (no spurious mirror)."""

    class _Act:
        def __init__(self):
            self.calls = []

        def set_lifecycle(self, state, reason):
            self.calls.append((state, reason))

    a = _Act()
    LifecycleState(activity=a)  # state=ACTIVE by default
    assert a.calls == []


def test_on_deprovision_fires_once_on_the_transition():
    """Issue #6: the entry revokes its island keys when the device is deprovisioned."""
    calls = []
    lc = LifecycleState(on_deprovision=lambda: calls.append(1))
    lc.pause("paused", "2026-06-25T09:00:00Z")
    assert calls == []
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    lc.deprovision("revoked again", "2026-06-25T11:00:00Z")
    assert calls == [1]


def test_on_deprovision_does_not_fire_for_a_seeded_deprovisioned_state():
    calls = []
    lc = LifecycleState(state="deprovisioned", on_deprovision=lambda: calls.append(1))
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    assert calls == []
