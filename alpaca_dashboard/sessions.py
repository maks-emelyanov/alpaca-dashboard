"""NYSE regular sessions used by account performance timeframes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Session:
    label: date
    open: datetime
    close: datetime


class SessionCalendar:
    def __init__(self) -> None:
        self.calendar = exchange_calendars.get_calendar(
            "XNYS", start="1990-01-01", end="2050-12-31"
        )
        self._sessions: dict[date, Session | None] = {}

    def session(self, day: date) -> Session | None:
        if day in self._sessions:
            return self._sessions[day]
        label = day.isoformat()
        if not self.calendar.is_session(label):
            self._sessions[day] = None
            return None
        session = Session(
            day,
            self.calendar.session_open(label).to_pydatetime(),
            self.calendar.session_close(label).to_pydatetime(),
        )
        self._sessions[day] = session
        return session
