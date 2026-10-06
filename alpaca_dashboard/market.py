"""Market transitions for dashboard refresh scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import timestamp
from .sessions import NEW_YORK
from .timeframes import _calendar


@dataclass(frozen=True)
class MarketSchedule:
    is_open: bool
    next_transition: datetime | None


def _clock_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None


def market_schedule(clock: dict, now: datetime | None = None) -> MarketSchedule:
    """Use the broker's next transition, then the NYSE calendar after it passes.

    A saved clock must not leave a dashboard permanently closed after its next
    open, or permanently open after its next close. Calendar fallback also covers
    weekends, holidays, daylight saving changes, and abbreviated sessions.
    """
    now = now or datetime.now(UTC)
    is_open = clock.get("is_open")
    observed = _clock_time(clock.get("timestamp"))
    boundary = _clock_time(clock.get("next_close" if is_open else "next_open"))
    if isinstance(is_open, bool) and boundary is not None and boundary > now:
        return MarketSchedule(is_open, boundary)
    if isinstance(is_open, bool) and boundary is not None and boundary <= now:
        following = _clock_time(clock.get("next_open" if is_open else "next_close"))
        if following is not None and following > now:
            return MarketSchedule(not is_open, following)
    # A minimal clock is useful for compatible broker adapters. Without either
    # a timestamp or boundary there is no evidence that its open flag is stale.
    if is_open is True and observed is None and boundary is None:
        return MarketSchedule(True, None)
    day = now.astimezone(NEW_YORK).date()
    calendar = _calendar()
    while True:
        session = calendar.session(day)
        if session is not None:
            if now < session.open:
                return MarketSchedule(False, session.open)
            if now < session.close and not (
                is_open is False and observed is None and boundary is None
            ):
                return MarketSchedule(True, session.close)
        day += timedelta(days=1)
