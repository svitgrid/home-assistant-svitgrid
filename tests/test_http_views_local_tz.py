"""The HTTP layer is where the household timezone enters the read path.

``hass.config.time_zone`` is the only place the household's wall clock is
known, so every day-scoped store call must carry it, and each intraday bucket
must ship the LOCAL hour it belongs to. The panel plots ``localHour``
directly; deriving it client-side from the UTC ``hour`` string is exactly the
bug this replaces.
"""

import json

import pytest

from custom_components.svitgrid.http_views import SvitgridHistoryView, SvitgridTodayView

KYIV = "Europe/Kyiv"  # UTC+3 in July


class _TzRecordingStore:
    """Records the tz each call receives and returns one UTC-stamped bucket."""

    def __init__(self):
        self.calls: dict[str, tuple] = {}

    async def hourly_range_live(self, inverter_id, day, tz_name=None):
        self.calls["hourly"] = (inverter_id, day, tz_name)
        return [
            {
                "hour": "2026-07-15T05:00:00Z",  # local 08:00 in Kyiv
                "sample_count": 10,
                "avgs": {"pvPower": 1900.0},
                "peaks": {},
                "energy": {},
            }
        ]

    async def five_min_range_live(self, inverter_id, day, tz_name=None):
        self.calls["five_min"] = (inverter_id, day, tz_name)
        return [
            {
                "hour": "2026-07-15T05:05:00Z",  # local 08:05 in Kyiv
                "sample_count": 3,
                "avgs": {},
                "peaks": {},
                "energy": {},
            }
        ]

    async def history_range_live(self, inverter_id, start, end, tz_name=None):
        self.calls["history"] = (inverter_id, start, end, tz_name)
        return [{"day": "2026-07-15", "sample_count": 5, "avgs": {}, "peaks": {}, "energy": {}}]

    async def today_summary(self, day, tz_name=None):
        self.calls["today"] = (day, tz_name)
        return [{"inverterId": "inv-1", "sample_count": 3, "peaks": {}, "energy": {}}]


class _FakeRequest:
    def __init__(self, hass, query=None):
        self.app = {"hass": hass}
        self.query = query if query is not None else {}
        self._data: dict = {"ha_authenticated": True}
        self.headers: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]


async def _get_json(view, request):
    resp = await view.get(request)
    assert resp.status == 200
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_hourly_buckets_carry_the_local_hour(hass):
    """The reported bug, at the wire boundary: a 05:00Z bucket must be
    labelled hour 8 for a Kyiv household, not 5."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 8
    assert body["hours"][0]["hour"] == "2026-07-15T05:00:00Z"  # UTC key preserved


@pytest.mark.asyncio
async def test_hourly_query_forwards_the_household_timezone(hass):
    """With the opt-in flag the window tz reaches the store."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(
        hass, query={"granularity": "hourly", "day": "2026-07-15", "local_day": "1"}
    )

    await SvitgridHistoryView(store).get(request)

    assert store.calls["hourly"] == ("", "2026-07-15", KYIV)


@pytest.mark.asyncio
async def test_five_min_buckets_carry_the_local_hour(hass):
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(
        hass, query={"granularity": "5min", "day": "2026-07-15", "local_day": "1"}
    )

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 8
    assert store.calls["five_min"] == ("", "2026-07-15", KYIV)


@pytest.mark.asyncio
async def test_daily_history_forwards_the_household_timezone(hass):
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"start": "2026-07-01", "end": "2026-07-31"})

    await SvitgridHistoryView(store).get(request)

    assert store.calls["history"] == ("", "2026-07-01", "2026-07-31", KYIV)


@pytest.mark.asyncio
async def test_today_view_uses_the_local_calendar_day(hass, monkeypatch):
    """At 2026-07-15T22:30Z it is already Jul 16 in Kyiv -- "today" must be
    Jul 16, or the household sees yesterday's summary for three hours every
    night."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()

    import custom_components.svitgrid.http_views as hv

    monkeypatch.setattr(hv, "_utc_now_iso", lambda: "2026-07-15T22:30:00Z")

    body = await _get_json(SvitgridTodayView(store), _FakeRequest(hass))

    assert body["day"] == "2026-07-16"
    assert store.calls["today"] == ("2026-07-16", KYIV)


@pytest.mark.asyncio
async def test_unset_timezone_degrades_to_utc(hass):
    """A household with no configured timezone keeps the old UTC behaviour
    rather than erroring."""
    hass.config.time_zone = None
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 5


# --------------------------------------------------------------------- #
# Opt-in gating: `local_day=1`
# --------------------------------------------------------------------- #
#
# The intraday `day=` window is a SHARED contract. The panel ships inside the
# add-on and moves in lockstep, but the mobile app is a separate release train
# (app-store review, then user updates) and the add-on self-updates via HACS
# within ~12h -- so the add-on always reaches households first.
#
# The app compensates for the UTC window client-side: it enumerates the UTC
# days its local range spans and merges. Silently switching this endpoint to
# local days makes those labels mean something else, and the app under-requests
# at the recent end -- blank Day charts every night between 00:00 and 03:00 for
# a UTC+3 household. So local windowing is OPT-IN, and only the panel opts in.
#
# `localHour` is emitted unconditionally: it is additive, derived per bucket
# from the bucket's own UTC key, and correct under either window. That lets the
# app adopt it later without another server change.


@pytest.mark.asyncio
async def test_local_day_window_is_opt_in(hass):
    """Without the flag the window stays UTC -- the pre-existing contract the
    mobile app is built against."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    await SvitgridHistoryView(store).get(request)

    assert store.calls["hourly"] == ("", "2026-07-15", None)


@pytest.mark.asyncio
async def test_local_day_flag_switches_the_window_to_local(hass):
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(
        hass, query={"granularity": "hourly", "day": "2026-07-15", "local_day": "1"}
    )

    await SvitgridHistoryView(store).get(request)

    assert store.calls["hourly"] == ("", "2026-07-15", KYIV)


@pytest.mark.asyncio
async def test_five_min_window_is_opt_in_too(hass):
    """The app's Day chart uses the 5min path, so it needs the same gate."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()

    await SvitgridHistoryView(store).get(
        _FakeRequest(hass, query={"granularity": "5min", "day": "2026-07-15"})
    )
    assert store.calls["five_min"] == ("", "2026-07-15", None)

    await SvitgridHistoryView(store).get(
        _FakeRequest(
            hass, query={"granularity": "5min", "day": "2026-07-15", "local_day": "1"}
        )
    )
    assert store.calls["five_min"] == ("", "2026-07-15", KYIV)


@pytest.mark.asyncio
async def test_local_hour_is_emitted_even_without_the_flag(hass):
    """Additive and window-independent, so the app can adopt it on its own
    schedule without a coordinated server release."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()
    request = _FakeRequest(hass, query={"granularity": "hourly", "day": "2026-07-15"})

    body = await _get_json(SvitgridHistoryView(store), request)

    assert body["hours"][0]["localHour"] == 8


@pytest.mark.asyncio
async def test_unflagged_day_default_stays_utc_today(hass, monkeypatch):
    """REGRESSION GUARD for the mobile break: at 01:30 local on Jul 15 (Kyiv)
    the app asks for UTC day Jul 14. That must keep returning the UTC day, or
    the app's window and the server's stop overlapping and its Day chart,
    hour drill-down and voltage charts go blank between 00:00 and 03:00."""
    hass.config.time_zone = KYIV
    store = _TzRecordingStore()

    import custom_components.svitgrid.http_views as hv

    monkeypatch.setattr(hv, "_utc_now_iso", lambda: "2026-07-14T22:30:00Z")

    await SvitgridHistoryView(store).get(
        _FakeRequest(hass, query={"granularity": "hourly"})
    )

    # UTC day, and a UTC window -- exactly what the app expects today.
    assert store.calls["hourly"] == ("", "2026-07-14", None)
