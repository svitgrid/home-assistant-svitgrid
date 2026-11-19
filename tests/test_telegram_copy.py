"""Island Telegram copy renderer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.svitgrid.telegram import copy

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
# 2026-10-30 09:05 UTC is 11:05 in Kyiv (EET, UTC+2, DST ended 2026-10-25).
TODAY_EVENT = datetime(2026, 10, 30, 9, 5, tzinfo=UTC)


def test_render_outage_uk():
    out = copy.render("outage", "uk", station="Дім", soc=78)
    assert out == ("<b>Відключення електроенергії</b>\nДім працює від батареї (78%)")


def test_render_outage_without_soc_leaves_the_figure_out():
    """No batterySoc (a battery with no BMS): no «(%)» (svitgrid D-2294)."""
    assert copy.render("outage", "uk", station="Дім") == (
        "<b>Відключення електроенергії</b>\nДім працює від батареї"
    )
    assert copy.render("outage", "en", station="Home", soc=None) == (
        "<b>Power Outage</b>\nHome is on battery power"
    )
    assert copy.render("outage", "tr", station="Ev").endswith("Ev batarya gücüyle çalışıyor")


@pytest.mark.parametrize("language", copy._FIXTURE["languages"])
def test_outage_without_soc_has_no_empty_figure_in_any_language(language):
    out = copy.render("outage", language, station="S")
    assert "%" not in out
    assert "()" not in out
    assert out == out.rstrip()


def test_render_outage_with_zero_soc_keeps_the_figure():
    assert copy.render("outage", "en", station="Home", soc=0).endswith("(0%)")


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


def test_offline_copy_carries_minutes():
    """The liveness messages (svitgrid D-2149) fill `{minutesAgo}`."""
    out = copy.render("offline_board", "uk", station="Дача", minutes_ago=16)
    assert out.startswith("<b>Модуль Svitgrid не на зв'язку</b>\nВід Дача немає даних 16 хв")
    assert copy.render("offline", "en", station="Home", minutes_ago=15) == (
        "<b>Inverter Offline</b>\nHome has been offline for 15 minutes"
    )
    assert copy.render("offline_board_recovered", "uk", station="Дача") == (
        "<b>Модуль Svitgrid знову на зв'язку</b>\nВід Дача знову надходять дані."
    )


def test_string_offline_copy_names_the_string():
    """The cloud's `string_offline` copy, with the inverter and the string (svitgrid D-2155)."""
    out = copy.render("string_offline", "uk", station="Deye <1>", string_id="PV2, PV3")
    assert out == (
        "<b>Рядок панелей не працює</b>\nDeye &lt;1&gt;: рядок PV2, PV3 майже не виробляє, "
        "тоді як інші працюють — перевірте підключення/запобіжники."
    )
    assert copy.render("string_offline", "en", station="Home", string_id="PV1").startswith(
        "<b>Solar string offline</b>\nHome: string PV1 is producing almost nothing"
    )


def test_rule_notice_copy():
    """A rule's start and stop notices (svitgrid D-2151)."""
    named = copy.render_rule_notice(
        "calendar_event_started", "uk", name="Бойлер", rule=True, end_time=None
    )
    assert named == ("<b>▶️ Подія «Бойлер» почалася</b>\nЗапланована автоматизація розпочалася")
    unnamed_rule = copy.render_rule_notice(
        "calendar_event_ended", "en", name=None, rule=True, end_time="18:00"
    )
    assert unnamed_rule == "<b>Rule triggered</b>\nA scheduled automation has completed"
    scheduled = copy.render_rule_notice(
        "calendar_event_started", "en", name=" ", rule=False, end_time="18:00"
    )
    assert scheduled == ("<b>Schedule Started</b>\nA scheduled automation has begun\nUntil: 18:00")
    escaped = copy.render_rule_notice(
        "calendar_event_started", "en", name="<i>x</i>", rule=True, end_time=None
    )
    assert "&lt;i&gt;x&lt;/i&gt;" in escaped
