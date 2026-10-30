"""Island Telegram copy renderer."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.svitgrid.telegram import copy

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
# 2026-10-30 09:05 UTC is 11:05 in Kyiv (EET, UTC+2, DST ended 2026-10-25).
TODAY_EVENT = datetime(2026, 10, 30, 9, 5, tzinfo=UTC)


def test_render_outage_uk():
    out = copy.render("outage", "uk", station="Дім", soc=78)
    assert out == ("<b>Відключення електроенергії</b>\nДім працює від батареї (78%)")


def test_render_falls_back_to_en_for_ja():
    out = copy.render("grid_restored", "ja", station="Home")
    assert out == "<b>Grid Power Restored</b>\nHome is back on grid power"


def test_uk_ua_resolves_to_uk():
    assert copy.render("outage", "uk-UA", station="A", soc=1) == copy.render(
        "outage", "uk", station="A", soc=1
    )
    assert copy.open_button("uk_UA") == "Відкрити в Svitgrid"
    assert copy.open_button(None) == "Open in Svitgrid"


def test_station_name_is_escaped():
    out = copy.render("outage", "en", station="<b>x</b> & y", soc=5)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in out
    assert "<b>x</b>" not in out


def test_linked_has_no_title_and_test_uses_test_copy():
    assert copy.render("linked", "en", station="S") == (
        "Done: this station's alerts will arrive here."
    )
    assert copy.render("test", "en", station="S").startswith("<b>Test notification</b>\n")


def test_event_time_in_station_zone_without_date_when_today():
    out = copy.render(
        "grid_restored",
        "en",
        station="S",
        event_at=TODAY_EVENT,
        tz="Europe/Kyiv",
        now=NOW,
    )
    assert out.endswith("\n🕑 11:05")


def test_event_time_shows_date_when_not_today():
    out = copy.render(
        "grid_restored",
        "en",
        station="S",
        event_at=datetime(2026, 10, 29, 21, 30, tzinfo=UTC),
        tz="Europe/Kyiv",
        now=NOW,
    )
    assert out.endswith("\n🕑 30.10 23:30") is False
    assert out.endswith("\n🕑 29.10 23:30")


def test_unknown_or_missing_tz_falls_back_to_utc():
    for tz in (None, "Not/AZone"):
        out = copy.render(
            "outage",
            "en",
            station="S",
            soc=1,
            event_at=TODAY_EVENT,
            tz=tz,
            now=NOW,
        )
        assert out.endswith("\n🕑 09:05")
