from datetime import UTC, datetime, timedelta

import pytest

from alpaca_dashboard.timeframes import PRESETS, resolve_window

INCEPTION = datetime(2020, 1, 1, tzinfo=UTC)


def now(value):
    return datetime.fromisoformat(value)


def test_weekend_and_premarket_select_previous_regular_session():
    for at in ("2026-09-26T16:00:00+00:00", "2026-09-28T12:00:00+00:00"):
        window = resolve_window("1D", now(at), INCEPTION)
        assert window.start == now("2026-09-25T13:30:00+00:00")
        assert window.end == now("2026-09-25T20:00:00+00:00") + timedelta(microseconds=1)
        assert not window.live
        assert window.resolution == "1Min"


def test_holiday_early_close_and_dst():
    thanksgiving = resolve_window("1D", now("2026-11-26T20:00:00+00:00"), INCEPTION)
    assert thanksgiving.start.day == 25
    friday = resolve_window("1D", now("2026-11-27T19:00:00+00:00"), INCEPTION)
    assert friday.start.hour == 14
    assert friday.end.hour == 18
    assert not friday.live
    summer = resolve_window("1D", now("2026-09-25T16:00:00+00:00"), INCEPTION)
    assert summer.start.hour == 13
    assert summer.live


def test_custom_is_inclusive_dates_and_handles_23_and_25_hour_days():
    clock = now("2026-12-01T16:00:00+00:00")
    spring = resolve_window("CUSTOM", clock, INCEPTION, "2026-03-08", "2026-03-08")
    fall = resolve_window("CUSTOM", clock, INCEPTION, "2026-11-01", "2026-11-01")
    assert spring.end - spring.start == timedelta(hours=23)
    assert fall.end - fall.start == timedelta(hours=25)
    assert spring.resolution == fall.resolution == "1Min"


def test_calendar_month_clamps_day_and_ytd_uses_new_york_midnight():
    clock = now("2024-03-31T16:00:00+00:00")
    month = resolve_window("1M", clock, INCEPTION)
    assert month.start == now("2024-02-29T05:00:00+00:00")
    year = resolve_window("YTD", clock, INCEPTION)
    assert year.start == now("2024-01-01T05:00:00+00:00")


@pytest.mark.parametrize("preset", PRESETS)
def test_all_presets_and_stable_keys(preset):
    clock = now("2026-09-25T16:00:00+00:00")
    args = ("2026-09-01", "2026-09-25") if preset == "CUSTOM" else ()
    first = resolve_window(preset, clock, INCEPTION, *args)
    second = resolve_window(preset, clock + timedelta(seconds=5), INCEPTION, *args)
    assert first.key == second.key
    assert first.start.tzinfo == UTC
    assert first.start < first.end
    if preset == "ALL":
        assert first.start == INCEPTION


@pytest.mark.parametrize(
    "first,last",
    [
        (None, None),
        ("2026-09-26", "2026-09-25"),
        ("bad", "2026-09-25"),
        ("2026-09-25", "2026-09-26"),
    ],
)
def test_invalid_custom_dates_are_rejected(first, last):
    with pytest.raises(ValueError):
        resolve_window("CUSTOM", now("2026-09-25T16:00:00+00:00"), INCEPTION, first, last)


@pytest.mark.parametrize(
    "first,last,resolution",
    [
        ("2026-09-01", "2026-09-01", "1Min"),
        ("2026-09-01", "2026-09-07", "5Min"),
        ("2026-09-01", "2026-09-30", "15Min"),
        ("2026-08-01", "2026-09-30", "1D"),
    ],
)
def test_resolution_boundaries(first, last, resolution):
    window = resolve_window("CUSTOM", now("2026-10-01T16:00:00+00:00"), INCEPTION, first, last)
    assert window.resolution == resolution


def test_market_close_is_inside_the_window_but_is_not_live():
    close = now("2026-09-25T20:00:00+00:00")
    window = resolve_window("1D", close, INCEPTION)
    assert window.start <= close < window.end
    assert not window.live
