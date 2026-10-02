"""Cash-flow-adjusted account performance from broker equity snapshots.

Alpaca's ``cashflow`` arrays contain amounts per timestamp bucket, not cumulative
running totals. Only external funding is removed: dividends, interest, fees and
financing remain part of account performance. No order-based P&L is used here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from alpaca_dashboard.sessions import NEW_YORK

from .accounting import SECURITY_EVENTS
from .models import ZERO, Window, decimal_text, number, timestamp
from .timeframes import _calendar

EXTERNAL_CASH = {"CSD", "CSW", "ACATC", "JNLC"}


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _activity_time(activity: dict) -> datetime | None:
    value = activity.get("transaction_time") or activity.get("timestamp")
    if not value and "T" in str(activity.get("date") or ""):
        value = activity["date"]
    if value is None:
        return None
    try:
        return timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _activity_flows(
    activities: list[dict],
    baseline: datetime,
    point: datetime,
    *,
    daily: bool,
) -> tuple[Decimal, str | None]:
    amount = ZERO
    for activity in activities:
        kind = activity.get("activity_type")
        if kind not in EXTERNAL_CASH | SECURITY_EVENTS:
            continue
        at = _activity_time(activity)
        day = _date(activity.get("date")) if at is None else at.astimezone(NEW_YORK).date()
        first_day = baseline.astimezone(NEW_YORK).date()
        last_day = point.astimezone(NEW_YORK).date()
        if at is not None:
            relevant = baseline < at <= point
        else:
            if day is None:
                return amount, "Funding activity is missing its date"
            relevant = first_day <= day <= last_day
            if not relevant:
                continue
            if day == first_day or (day == last_day and not daily):
                return amount, "Funding time is unavailable for this intraday interval"
        if not relevant:
            continue
        if kind in SECURITY_EVENTS:
            return amount, "Security transfer or corporate action needs valuation adjustments"
        value = number(activity.get("net_amount"))
        if value is None:
            return amount, "Funding amount is unavailable"
        amount += value
    return amount, None


def _baseline(raw: list[dict], history: dict, window: Window) -> tuple[Decimal, datetime, int]:
    before = [item for item in raw if item["at"] < window.start]
    if before:
        item = before[-1]
        return item["equity"], item["at"], item["index"]
    value = number(history.get("base_value"))
    asof = _date(history.get("base_value_asof"))
    if value is not None and asof is not None:
        session = _calendar().session(asof)
        at = session.close if session else datetime.combine(asof, time.max, NEW_YORK)
        if at < raw[0]["at"]:
            return value, at.astimezone(UTC), -1
    item = next((item for item in raw if item["equity"] != 0), raw[0])
    return item["equity"], item["at"], item["index"]


def _cashflow_buckets(history: dict, length: int) -> tuple[list[Decimal] | None, str | None]:
    if "cashflow" not in history:
        return None, None
    values = history["cashflow"]
    if not isinstance(values, dict):
        return None, "Portfolio funding adjustments are unavailable"
    totals = [ZERO for _ in range(length)]
    for kind in EXTERNAL_CASH:
        if kind not in values:
            continue
        buckets = values[kind]
        if not isinstance(buckets, list) or len(buckets) != length:
            return None, "Portfolio funding adjustments do not match equity history"
        for index, raw in enumerate(buckets):
            amount = number(raw)
            if amount is None:
                return None, "Portfolio funding adjustments are incomplete"
            totals[index] += amount
    return totals, None


def _initial_funding(
    history: dict,
    raw: list[dict],
    activities: list[dict],
    inception: datetime,
    baseline: Decimal,
    baseline_index: int,
    buckets: list[Decimal] | None,
) -> tuple[int | None, Decimal, str | None]:
    """Reconcile capital backdated into equity before account creation.

    Paper history can include the initial balance before the account exists,
    with its funding in the baseline bucket or a later bucket. Confirm that
    capital against both broker sources and exclude it exactly once from cash flows.
    Keep the funded baseline, including any profit earned before posting.
    """
    unavailable = "Initial funding baseline cannot be reconciled with portfolio history"
    anchor = next((item for item in raw if item["equity"] != ZERO), None)
    if (
        buckets is None
        or number(history.get("base_value")) != baseline
        or anchor is None
        or anchor["equity"] != baseline
        or anchor["at"] >= inception
    ):
        return None, ZERO, unavailable
    creation_day = inception.astimezone(NEW_YORK).date()
    candidates = []
    for activity in activities:
        if activity.get("activity_type") not in EXTERNAL_CASH:
            continue
        at = _activity_time(activity)
        day = at.astimezone(NEW_YORK).date() if at else _date(activity.get("date"))
        if day == creation_day and number(activity.get("net_amount")) == baseline:
            candidates.append(activity)
    if len(candidates) != 1:
        return None, ZERO, unavailable
    activity = candidates[0]
    values = history["cashflow"].get(activity["activity_type"], [])
    matches = [
        item["index"]
        for item in raw
        if item["index"] >= baseline_index
        and item["at"].astimezone(NEW_YORK).date() == creation_day
        and item["index"] < len(values)
        and number(values[item["index"]]) == baseline
    ]
    if len(matches) != 1:
        return None, ZERO, unavailable
    if matches[0] == baseline_index:
        # Performance already excludes cash flows in the baseline bucket.
        # Applying the late-posting adjustment here would count the seed twice.
        return None, ZERO, None
    return matches[0], baseline, None


def _live_flows(
    activities: list[dict],
    last: datetime,
    now: datetime,
    raw: list[dict],
    buckets: list[Decimal],
) -> tuple[Decimal, str | None]:
    """Apply only confirmed post-history funding, checking for late postings.

    A newly observed activity can predate the newest cached history timestamp.
    Comparing covered-day ledger totals catches that race before current equity
    is mistaken for trading profit. Portfolio bucket totals are never subtracted
    a second time from an already adjusted historical point.
    """
    exact = [item for item in activities if _activity_time(item) is not None]
    amount, error = _activity_flows(exact, last, now, daily=False)
    if error:
        return amount, error
    dated = [
        item
        for item in activities
        if _activity_time(item) is None
        and item.get("activity_type") in EXTERNAL_CASH | SECURITY_EVENTS
    ]
    if any(_date(item.get("date")) is None for item in dated):
        return amount, "Funding activity is missing its date"
    covered_days = {item["at"].astimezone(NEW_YORK).date() for item in raw}
    for day in covered_days:
        exact_items = [
            item
            for item in exact
            if item.get("activity_type") in EXTERNAL_CASH
            and _activity_time(item) <= last
            and _activity_time(item).astimezone(NEW_YORK).date() == day
        ]
        dated_items = [
            item
            for item in dated
            if _date(item.get("date")) == day and item.get("activity_type") in EXTERNAL_CASH
        ]
        values = [number(item.get("net_amount")) for item in [*exact_items, *dated_items]]
        if any(value is None for value in values):
            return amount, "Funding amount is unavailable"
        confirmed = sum(
            (
                buckets[item["index"]]
                for item in raw
                if item["at"].astimezone(NEW_YORK).date() == day
            ),
            ZERO,
        )
        if sum(values, ZERO) != confirmed:
            if dated_items:
                return amount, "New funding time is unavailable until portfolio history refreshes"
            return amount, "Funding ledger and portfolio history disagree; waiting for refresh"
    last_day = last.astimezone(NEW_YORK).date()
    today = now.astimezone(NEW_YORK).date()
    for day in {day for item in dated if (day := _date(item.get("date"))) is not None}:
        if not last_day <= day <= today:
            continue
        items = [item for item in dated if _date(item.get("date")) == day]
        if any(item.get("activity_type") in SECURITY_EVENTS for item in items):
            return amount, "Security transfer or corporate action needs valuation adjustments"
        if day == last_day:
            continue  # Already reconciled with the cached cashflow buckets above.
        amounts = [number(item.get("net_amount")) for item in items]
        if any(value is None for value in amounts):
            return amount, "Funding amount is unavailable"
        if day < today:
            amount += sum(amounts, ZERO)
        else:
            return amount, "New funding time is unavailable until portfolio history refreshes"
    return amount, None


def performance_series(
    history: dict | None,
    window: Window,
    *,
    account: dict | None = None,
    activities: Any = (),
    as_of: datetime | None = None,
    history_complete: bool = True,
) -> dict:
    """Return decimal-string points and period values, explicitly marking gaps.

    ``history_complete`` means the activity ledger covers account inception. A
    typed cashflow response can independently establish historical funding, but
    live tails require the complete ledger to prove that no new funding exists.
    """
    history = history or {}
    activities = list({str(item.get("id") or repr(item)): item for item in activities}.values())
    now = timestamp(as_of or datetime.now(UTC))
    stamps, equities = history.get("timestamp") or [], history.get("equity") or []
    malformed = not isinstance(stamps, list) or not isinstance(equities, list)
    if malformed:
        return {
            "points": [],
            "pnl": None,
            "pnl_pct": None,
            "error": "Portfolio equity history is incomplete",
        }
    malformed = len(stamps) != len(equities)
    raw = []
    for index, (stamp, equity) in enumerate(zip(stamps, equities, strict=False)):
        try:
            if isinstance(stamp, bool):
                raise ValueError
            at, value = timestamp(stamp), number(equity)
        except (ValueError, TypeError, OverflowError):
            malformed = True
            continue
        if at > now:
            continue
        if value is None:
            malformed = True
            continue
        if raw and at <= raw[-1]["at"]:
            malformed = True
        raw.append({"at": at, "equity": value, "index": index})
    raw.sort(key=lambda item: item["at"])
    if not raw:
        return {
            "points": [],
            "pnl": None,
            "pnl_pct": None,
            "error": "Portfolio equity history is incomplete"
            if malformed
            else "Portfolio history is unavailable",
        }
    baseline, baseline_at, baseline_index = _baseline(raw, history, window)
    buckets, bucket_error = _cashflow_buckets(history, len(stamps))
    if malformed:
        bucket_error = "Portfolio equity history is incomplete"
    try:
        created_at = (account or {}).get("created_at")
        inception = timestamp(created_at) if created_at else None
    except (ValueError, TypeError, OverflowError):
        inception = None
    seed_index, seed_amount = None, ZERO
    if (
        not bucket_error
        and inception is not None
        and window.start <= inception < window.end
        and baseline_at < inception
        and baseline > ZERO
    ):
        seed_index, seed_amount, bucket_error = _initial_funding(
            history, raw, activities, inception, baseline, baseline_index, buckets
        )
    points = []
    note = None
    errors = [bucket_error] if bucket_error else []
    for item in raw:
        if (
            item["at"] < window.start
            or item["at"] >= window.end
            or (inception is not None and item["at"] < inception)
        ):
            continue
        if item["at"] < baseline_at:
            started = baseline_at.astimezone(NEW_YORK).strftime("%b %d, %Y · %I:%M %p ET")
            note = (
                f"P&L starts at the first recorded nonzero balance ({started}). "
                "Earlier zero-balance history is excluded."
            )
            points.append(_point(item["at"], item["equity"], baseline, ZERO, note))
            continue
        error = bucket_error
        flow = ZERO
        if buckets is not None:
            flow = sum(buckets[max(0, baseline_index + 1) : item["index"] + 1], ZERO)
            if seed_index is not None and item["index"] >= seed_index:
                flow -= seed_amount
            _, security_error = _activity_flows(
                [
                    activity
                    for activity in activities
                    if activity.get("activity_type") in SECURITY_EVENTS
                ],
                baseline_at,
                item["at"],
                daily=window.resolution == "1D",
            )
            error = error or security_error
        elif not history_complete:
            error = error or "Funding history is incomplete"
        else:
            flow, funding_error = _activity_flows(
                activities,
                baseline_at,
                item["at"],
                daily=window.resolution == "1D",
            )
            error = error or funding_error
        points.append(_point(item["at"], item["equity"], baseline, flow, error))
        if error:
            errors.append(error)
    latest = raw[-1]
    equity = number((account or {}).get("equity"))
    if window.live and window.start <= now < window.end and equity is not None:
        error = bucket_error
        if now < baseline_at:
            error = "Historical baseline is unavailable"
        if buckets is not None:
            flow = sum(buckets[max(0, baseline_index + 1) : latest["index"] + 1], ZERO)
            if seed_index is not None and latest["index"] >= seed_index:
                flow -= seed_amount
            added, funding_error = _live_flows(activities, latest["at"], now, raw, buckets)
            flow += added
            error = error or funding_error
        else:
            flow, funding_error = _activity_flows(activities, baseline_at, now, daily=False)
            error = error or funding_error
        if not history_complete:
            error = error or "Live funding history is incomplete"
        # A known security adjustment invalidates the period, including its live tail.
        _, security_error = _activity_flows(
            [item for item in activities if item.get("activity_type") in SECURITY_EVENTS],
            baseline_at,
            now,
            daily=False,
        )
        error = error or security_error
        point = _point(now, equity, baseline, flow, error)
        if points and points[-1]["timestamp"] == now.isoformat():
            points[-1] = point
        else:
            points.append(point)
        if error:
            errors.append(error)
    last = points[-1] if points else {}
    return {
        "points": points,
        "pnl": last.get("pnl"),
        "pnl_pct": last.get("pnl_pct"),
        "note": note,
        "error": "; ".join(dict.fromkeys(errors))
        or (None if points else "No portfolio history is available in this timeframe"),
    }


def _point(
    at: datetime,
    equity: Decimal,
    baseline: Decimal,
    flow: Decimal,
    error: str | None,
) -> dict:
    pnl = equity - baseline - flow if error is None else None
    pct = pnl / baseline * 100 if pnl is not None and baseline != 0 else None
    return {
        "timestamp": at.isoformat(),
        "equity": str(equity),
        "pnl": decimal_text(pnl),
        "pnl_pct": decimal_text(pct),
    }
