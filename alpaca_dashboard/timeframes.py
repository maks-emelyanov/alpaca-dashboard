"""One set of calendar-aware range rules for the chart and trade history."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from alpaca_dashboard.sessions import NEW_YORK, SessionCalendar

from .models import Window, timestamp

PRESETS = ("1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "ALL", "CUSTOM")


@lru_cache(maxsize=1)
def _calendar() -> SessionCalendar:
    return SessionCalendar()


def _date(value: date | str | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        raise ValueError("Choose both a start and an end date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Dates must use YYYY-MM-DD") from None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), NEW_YORK).astimezone(UTC)


def _months_before(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def resolve_window(
    preset: str,
    now: datetime,
    inception: datetime,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> Window:
    """Resolve a selection, with exchange sessions for 1D and NY calendar dates.

    Stable keys intentionally omit the moving clock, preserving chart zoom while
    polling. The service additionally keys history fetches by resolved dates.
    """
    preset = preset.upper()
    if preset not in PRESETS:
        raise ValueError("Unknown timeframe")
    now, inception = timestamp(now), timestamp(inception)
    local = now.astimezone(NEW_YORK)
    today = local.date()
    end = now + timedelta(microseconds=1)
    live = True
    key = preset
    if preset == "1D":
        calendar = _calendar()
        day = today
        session = calendar.session(day)
        while session is None or session.open > now:
            day -= timedelta(days=1)
            session = calendar.session(day)
        start = session.open.astimezone(UTC)
        end = session.close.astimezone(UTC) + timedelta(microseconds=1)
        live = start <= now < session.close
        key = f"1D:{session.label.isoformat()}"
    elif preset == "CUSTOM":
        first, last = _date(start_date), _date(end_date)
        if first > last:
            raise ValueError("Start date must be on or before the end date")
        if last > today:
            raise ValueError("The end date cannot be in the future")
        start, end = _midnight(first), _midnight(last + timedelta(days=1))
        live = first <= today <= last
        key = f"CUSTOM:{first.isoformat()}:{last.isoformat()}"
    elif preset == "1W":
        start = timestamp(local - timedelta(days=7))
    elif preset in {"1M", "3M", "6M", "1Y"}:
        months = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}[preset]
        start = _midnight(_months_before(today, months))
    elif preset == "YTD":
        start = _midnight(date(today.year, 1, 1))
    else:
        start = inception
    duration = end - start
    if duration <= timedelta(days=1, hours=1):
        resolution = "1Min"
    elif duration <= timedelta(days=7):
        resolution = "5Min"
    elif duration <= timedelta(days=30):
        resolution = "15Min"
    else:
        resolution = "1D"
    # The moving upper boundary is open; its microsecond must not promote a week.
    if preset == "1W":
        resolution = "5Min"
    return Window(start, end, resolution, key, live, preset)
