from datetime import UTC, datetime

import pytest

from alpaca_dashboard.market import market_schedule


def at(value):
    return datetime.fromisoformat(value).astimezone(UTC)


@pytest.mark.parametrize(
    ("now", "is_open", "transition"),
    [
        ("2026-10-05T09:29:59-04:00", False, "2026-10-05T09:30:00-04:00"),
        ("2026-10-05T09:30:00-04:00", True, "2026-10-05T16:00:00-04:00"),
        ("2026-10-05T16:00:00-04:00", False, "2026-10-06T09:30:00-04:00"),
        ("2026-07-03T10:00:00-04:00", False, "2026-07-06T09:30:00-04:00"),
        ("2026-11-27T12:59:59-05:00", True, "2026-11-27T13:00:00-05:00"),
        ("2026-11-27T13:00:00-05:00", False, "2026-11-30T09:30:00-05:00"),
        ("2026-03-06T16:00:00-05:00", False, "2026-03-09T09:30:00-04:00"),
        ("2026-10-30T16:00:00-04:00", False, "2026-11-02T09:30:00-05:00"),
    ],
)
def test_calendar_fallback_observes_sessions_holidays_early_closes_and_dst(
    now, is_open, transition
):
    schedule = market_schedule({}, at(now))
    assert schedule.is_open is is_open
    assert schedule.next_transition == at(transition)


def test_broker_clock_can_override_the_calendar_until_its_next_transition():
    # An unexpected closure reported by the broker takes precedence over the
    # regular calendar, without requiring repeated clock requests while closed.
    clock = {
        "timestamp": "2026-10-05T10:00:00-04:00",
        "is_open": False,
        "next_open": "2026-10-06T09:30:00-04:00",
    }
    schedule = market_schedule(clock, at("2026-10-05T11:00:00-04:00"))
    assert not schedule.is_open
    assert schedule.next_transition == at(clock["next_open"])


@pytest.mark.parametrize(
    ("clock", "now", "is_open", "transition"),
    [
        (
            {"is_open": False, "next_open": "2026-10-05T09:30:00-04:00"},
            "2026-10-05T09:30:00-04:00",
            True,
            "2026-10-05T16:00:00-04:00",
        ),
        (
            {"is_open": True, "next_close": "2026-10-05T16:00:00-04:00"},
            "2026-10-05T16:00:00-04:00",
            False,
            "2026-10-06T09:30:00-04:00",
        ),
        (
            {"timestamp": "2026-10-02T11:00:00-04:00", "is_open": True},
            "2026-10-03T12:00:00-04:00",
            False,
            "2026-10-05T09:30:00-04:00",
        ),
    ],
)
def test_expired_or_incomplete_clocks_do_not_leave_the_schedule_stuck(
    clock, now, is_open, transition
):
    schedule = market_schedule(clock, at(now))
    assert schedule.is_open is is_open
    assert schedule.next_transition == at(transition)


def test_minimal_open_clock_remains_compatible_with_broker_adapters():
    schedule = market_schedule({"is_open": True}, at("2026-10-03T12:00:00-04:00"))
    assert schedule.is_open
    assert schedule.next_transition is None


def test_minimal_closed_clock_does_not_resume_during_the_current_session():
    schedule = market_schedule({"is_open": False}, at("2026-10-05T12:00:00-04:00"))
    assert not schedule.is_open
    assert schedule.next_transition == at("2026-10-06T09:30:00-04:00")


def test_expired_broker_boundary_uses_the_following_broker_transition():
    schedule = market_schedule(
        {
            "is_open": True,
            "timestamp": "2026-10-05T12:59:00-04:00",
            "next_close": "2026-10-05T13:00:00-04:00",
            "next_open": "2026-10-06T09:30:00-04:00",
        },
        at("2026-10-05T13:00:00-04:00"),
    )
    assert not schedule.is_open
    assert schedule.next_transition == at("2026-10-06T09:30:00-04:00")

    schedule = market_schedule(
        {
            "is_open": False,
            "timestamp": "2026-10-05T09:29:00-04:00",
            "next_open": "2026-10-05T09:30:00-04:00",
            "next_close": "2026-10-05T13:00:00-04:00",
        },
        at("2026-10-05T09:30:00-04:00"),
    )
    assert schedule.is_open
    assert schedule.next_transition == at("2026-10-05T13:00:00-04:00")
