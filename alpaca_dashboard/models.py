"""Small, dependency-free types shared by dashboard data and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")


@dataclass(frozen=True)
class Window:
    """A UTC interval; custom end dates resolve to the following midnight."""

    start: datetime
    end: datetime
    resolution: str
    key: str
    live: bool
    preset: str


def timestamp(value: Any) -> datetime:
    """Parse broker timestamps without ever assuming the host's local timezone."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float, Decimal)):
        result = datetime.fromtimestamp(value, UTC)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("A timestamp with a timezone is required")
    return result.astimezone(UTC)


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
