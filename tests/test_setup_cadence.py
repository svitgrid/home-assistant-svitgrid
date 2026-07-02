from custom_components.svitgrid.readings_publisher import _clamp_interval, _INTERVAL_FLOOR_S


def test_floor_allows_five_seconds():
    assert _INTERVAL_FLOOR_S == 5
    assert _clamp_interval(5) == 5.0
    assert _clamp_interval(3) == 5.0  # below floor still clamps up


def test_cadence_initialized_from_entry_data():
    from custom_components.svitgrid.reading_sender import Cadence
    from custom_components.svitgrid import _initial_cadence_seconds
    # helper reads entry.data with a 300 fallback and clamps to a valid preset-range int
    assert _initial_cadence_seconds({"harvest_interval_seconds": 15}) == 15
    assert _initial_cadence_seconds({}) == 300
    assert _initial_cadence_seconds({"harvest_interval_seconds": 99999}) == 1800  # ceiling
