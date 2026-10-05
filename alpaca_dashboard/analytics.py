"""Strategy diagnostics from completed daily equity and closed trade lifecycles.

Daily returns use (ending equity - starting equity - external flows) / starting
equity. Linking them is an end-of-period cash-flow approximation, not exact TWR:
valuations at every deposit/withdrawal would be needed for that. The existing
performance module remains the authority for funding and initial-seed validation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from exchange_calendars.errors import DateOutOfBounds

from .models import ZERO, Window, number, timestamp
from .performance import _baseline, performance_series
from .sessions import NEW_YORK
from .timeframes import _calendar

ONE = Decimal(1)
HUNDRED = Decimal(100)
TRADING_DAYS = Decimal(252)
YEAR_DAYS = Decimal("365.25")

# Keep this small schema reusable by the UI and any future export.
DEFINITIONS = {
    "win_rate": (
        "Win rate",
        "trades",
        "percent",
        "Winning completed trades / all completed trades, including breakevens. Gross of fees.",
    ),
    "sharpe": (
        "Sharpe ratio",
        "equity",
        "ratio",
        "Mean daily return / sample daily standard deviation × √252. Risk-free rate: 0%.",
    ),
    "cagr": (
        "CAGR",
        "equity",
        "percent",
        "Linked daily wealth annualized over elapsed calendar years (365.25 days). "
        "Requires at least 365 days of observations; funding timing is approximated.",
    ),
    "max_drawdown": (
        "Max drawdown",
        "equity",
        "percent",
        "Largest peak-to-trough decline in linked daily wealth, shown as a positive loss. "
        "Intraday drawdowns are not measured.",
    ),
    "total_return": (
        "Total return",
        "equity",
        "percent",
        "Compounded daily (change in equity minus external flows) / prior equity. "
        "Assumes cash flows occur at period end; this is an estimate, not exact TWR.",
    ),
    "sortino": (
        "Sortino ratio",
        "equity",
        "ratio",
        "Mean daily return / root mean square of negative daily returns × √252. "
        "Target return: 0%; downside average includes all observed days.",
    ),
    "volatility": (
        "Annualized volatility",
        "equity",
        "percent",
        "Sample daily return standard deviation × √252. Requires two daily returns.",
    ),
    "calmar": (
        "Calmar ratio",
        "equity",
        "ratio",
        "CAGR / maximum daily drawdown over the observed period. "
        "Requires at least 365 days and a nonzero drawdown.",
    ),
    "trade_count": (
        "Completed trades",
        "trades",
        "count",
        "Complete zero-to-zero position lifecycles closed within the selected interval.",
    ),
    "profit_factor": (
        "Profit factor",
        "trades",
        "ratio",
        "Sum of gross winning trade P&L / absolute sum of gross losing trade P&L.",
    ),
    "expectancy": (
        "Expectancy / trade",
        "trades",
        "money",
        "Mean gross P&L per completed trade, including breakevens. Trade sizes are not normalized.",
    ),
    "payoff_ratio": (
        "Payoff ratio",
        "trades",
        "ratio",
        "Average gross winning trade / absolute average gross losing trade.",
    ),
    "average_win": (
        "Average win",
        "trades",
        "money",
        "Mean gross P&L of winning completed trades.",
    ),
    "average_loss": (
        "Average loss",
        "trades",
        "money",
        "Mean gross P&L of losing completed trades (negative).",
    ),
    "gross_pnl": (
        "Gross trade P&L",
        "trades",
        "money",
        "Sum of completed trade execution P&L. Excludes open positions and unallocated fees.",
    ),
    "average_duration": (
        "Average holding time",
        "trades",
        "seconds",
        "Mean elapsed time from first entry to final exit of completed trades.",
    ),
}


def analysis_window(window: Window) -> Window:
    """Request daily bars plus the preceding session needed as a return anchor."""
    day = window.start.astimezone(NEW_YORK).date() - timedelta(days=1)
    try:
        while _calendar().session(day) is None:
            day -= timedelta(days=1)
    except DateOutOfBounds:
        # Range controls also accept dates older than our session calendar.
        # Let the analytics response explain unavailable coverage to the UI.
        return replace(window, resolution="1D", key=f"analytics:{window.key}")
    return replace(
        window,
        start=datetime.combine(day, time(), NEW_YORK).astimezone(UTC),
        resolution="1D",
        key=f"analytics:{window.key}",
    )


def _set(result: dict, key: str, value: Decimal | int | None, reason: str | None = None) -> None:
    result["metrics"][key].update(
        value=str(value) if value is not None else None,
        status="available" if value is not None else "unavailable",
        reason=reason if value is None else None,
    )


def _unavailable(result: dict, group: str, reason: str) -> None:
    for key, metric in result["metrics"].items():
        if metric["group"] == group:
            _set(result, key, None, reason)


def _closed_trades(result: dict, trades: Any, window: Window, complete: bool) -> None:
    included, excluded, undated = [], 0, 0
    for trade in trades:
        try:
            closed = timestamp(trade.get("closed_at"))
        except (ValueError, TypeError, OverflowError):
            undated += 1
            continue
        if not window.start <= closed < window.end:
            continue
        pnl = number(trade.get("realized_pnl"))
        if trade.get("status") != "Completed" or pnl is None:
            excluded += 1
            continue
        duration = number(trade.get("duration_seconds"))
        included.append((pnl, duration if duration is not None and duration >= ZERO else None))

    values = [item[0] for item in included]
    wins = [value for value in values if value > ZERO]
    losses = [value for value in values if value < ZERO]
    count = len(values)
    result["coverage"].update(
        valid_trade_count=count,
        wins=len(wins),
        losses=len(losses),
        breakeven=count - len(wins) - len(losses),
        excluded=excluded,
        undated=undated,
    )
    if undated:
        result["notes"].append(
            f"{undated} incomplete trade record(s) have no usable exit time; their relationship "
            "to the selected period is unknown and the trade sample may be incomplete."
        )
    if excluded:
        result["notes"].append(
            f"{excluded} closed trade record(s) excluded because execution P&L is incomplete."
        )
    if not complete:
        _unavailable(result, "trades", "Account execution history is incomplete.")
        return
    _set(result, "trade_count", count)
    if not count:
        return
    if count < 30:
        result["notes"].append(
            "Fewer than 30 completed trades; trade statistics have a small sample."
        )
    gross = sum(values, ZERO)
    win_sum, loss_sum = sum(wins, ZERO), sum(losses, ZERO)
    avg_win = win_sum / len(wins) if wins else None
    avg_loss = loss_sum / len(losses) if losses else None
    _set(result, "win_rate", Decimal(len(wins)) / count * HUNDRED)
    _set(result, "gross_pnl", gross)
    _set(result, "expectancy", gross / count)
    _set(result, "average_win", avg_win, "No winning completed trades.")
    _set(result, "average_loss", avg_loss, "No losing completed trades.")
    _set(
        result,
        "profit_factor",
        win_sum / -loss_sum if losses else None,
        "No losing completed trades; profit factor is undefined.",
    )
    _set(
        result,
        "payoff_ratio",
        avg_win / -avg_loss if wins and losses else None,
        "Requires both winning and losing completed trades.",
    )
    durations = [duration for _, duration in included if duration is not None]
    _set(
        result,
        "average_duration",
        sum(durations, ZERO) / count if len(durations) == count else None,
        "Holding time is incomplete for one or more completed trades.",
    )


def _daily_history(history: dict, cutoff: datetime) -> tuple[dict, list[dict], str | None]:
    """Translate daily bucket labels to valuation closes, preserving bucket indices.

    Alpaca labels the beginning of a bucket, while equity is its ending value.
    Session dates use New York time. Cached bars remain partial until their own
    fetch timestamp has passed the exchange close (including early closes).
    """
    stamps, equities = history.get("timestamp") or [], history.get("equity") or []
    if (
        not isinstance(stamps, list)
        or not isinstance(equities, list)
        or len(stamps) != len(equities)
    ):
        return history, [], "Portfolio equity history is incomplete."
    normalized, raw = [], []
    previous = None
    for index, (stamp, equity) in enumerate(zip(stamps, equities, strict=True)):
        try:
            if isinstance(stamp, bool):
                raise ValueError
            at, value = timestamp(stamp), number(equity)
            if value is None:
                raise ValueError
            session = _calendar().session(at.astimezone(NEW_YORK).date())
        except (ValueError, TypeError, OverflowError):
            return history, [], "Portfolio equity history is incomplete."
        # Zero prefunding rows can occur on non-session days in paper history.
        if session is None and value != ZERO:
            return history, [], "Daily history contains a non-session equity snapshot."
        at = session.close if session else at
        if previous is not None and at <= previous:
            return history, [], "Daily history contains duplicate or unordered session snapshots."
        previous = at
        normalized.append(at.timestamp())
        if at <= cutoff:
            raw.append({"at": at, "equity": value, "index": index})
    return {**history, "timestamp": normalized}, raw, None


def _previous_session_close(at: datetime) -> datetime:
    day = at.astimezone(NEW_YORK).date() - timedelta(days=1)
    session = _calendar().session(day)
    while session is None:
        day -= timedelta(days=1)
        session = _calendar().session(day)
    return session.close


def _daily_returns(
    result: dict,
    history: dict,
    window: Window,
    account: dict,
    activities: Any,
    cutoff: datetime,
    history_complete: bool,
) -> list[Decimal] | None:
    normalized, raw, error = _daily_history(history, cutoff)
    if error:
        _unavailable(result, "equity", error)
        return None
    daily = replace(window, resolution="1D", live=False)
    series = performance_series(
        normalized,
        daily,
        account=account,
        activities=activities,
        as_of=cutoff,
        history_complete=history_complete,
    )
    if series.get("error"):
        _unavailable(result, "equity", series["error"])
        return None
    if series.get("note"):
        result["notes"].append(series["note"])
    if not raw:
        return None
    base_equity, base_at, _ = _baseline(raw, normalized, daily)
    try:
        inception = timestamp(account["created_at"]) if account.get("created_at") else None
    except (ValueError, TypeError, OverflowError):
        _unavailable(result, "equity", "Account inception time is invalid.")
        return None

    first_at = timestamp(series["points"][0]["timestamp"]) if series["points"] else None
    beginning = max(window.start, inception) if inception else window.start
    day = beginning.astimezone(NEW_YORK).date()
    while day <= cutoff.astimezone(NEW_YORK).date():
        session = _calendar().session(day)
        if session and beginning <= session.close <= cutoff:
            if first_at is not None and first_at > session.close:
                _unavailable(result, "equity", "Daily history is missing a required session close.")
                return None
            break
        day += timedelta(days=1)

    if first_at == base_at:
        previous_close = _previous_session_close(first_at)
        new_account = inception is not None and inception > previous_close
        zero_prefunding = any(item["equity"] == ZERO and item["at"] < base_at for item in raw)
        if not new_account and not zero_prefunding:
            _unavailable(result, "equity", "Daily history is missing its starting session close.")
            return None

    anchor = {"at": base_at, "equity": base_equity, "pnl": ZERO}
    if inception is not None and base_at < inception:
        anchor = None
    returns, start, last = [], None, None
    skipped = False
    for point in series["points"]:
        at = timestamp(point["timestamp"])
        equity, pnl = number(point["equity"]), number(point["pnl"])
        session = _calendar().session(at.astimezone(NEW_YORK).date())
        if session is None or pnl is None or equity is None:
            # performance_series explicitly identifies zero prefunding rows.
            if equity == ZERO and not returns:
                continue
            _unavailable(result, "equity", "Daily equity or funding adjustments are incomplete.")
            return None
        current = {"at": at, "equity": equity, "pnl": pnl}
        if anchor is None or at == anchor["at"] or session.open < window.start:
            anchor = current
            skipped = True
            continue
        previous_close = _previous_session_close(at)
        if anchor["at"] != previous_close:
            _unavailable(result, "equity", "Daily history is missing a required session close.")
            return None
        if anchor["equity"] <= ZERO:
            # Funding into an empty account establishes capital, not a return.
            if not returns and equity > ZERO:
                anchor = current
                skipped = True
                continue
            _unavailable(result, "equity", "Daily returns require positive starting equity.")
            return None
        value = (pnl - anchor["pnl"]) / anchor["equity"]
        if equity < ZERO or value <= -ONE:
            _unavailable(result, "equity", "Linked returns require strictly positive wealth.")
            return None
        start = start or anchor["at"]
        returns.append(value)
        last, anchor = at, current

    if skipped:
        result["notes"].append(
            "The first usable session close establishes the baseline; initial partial sessions "
            "are excluded from daily metrics."
        )
    if not returns:
        return []
    # A stale result must not look like complete coverage through a newer close.
    last_day = min(cutoff, window.end - timedelta(microseconds=1)).astimezone(NEW_YORK).date()
    day = last.astimezone(NEW_YORK).date() + timedelta(days=1)
    while day <= last_day:
        session = _calendar().session(day)
        if session and session.close <= cutoff and session.close < window.end:
            _unavailable(result, "equity", "Daily history is missing a required session close.")
            return None
        day += timedelta(days=1)
    result["coverage"].update(used_days=len(returns), start=start.isoformat(), end=last.isoformat())
    return returns


def _equity_metrics(result: dict, returns: list[Decimal]) -> None:
    wealth, peak, drawdown = ONE, ONE, ZERO
    for value in returns:
        wealth *= ONE + value
        peak = max(peak, wealth)
        drawdown = max(drawdown, (peak - wealth) / peak)
    _set(result, "total_return", (wealth - ONE) * HUNDRED)
    _set(result, "max_drawdown", drawdown * HUNDRED)
    count = len(returns)
    if count < 30:
        result["notes"].append("Fewer than 30 daily returns; risk estimates have a small sample.")
    if count < 252:
        result["notes"].append(
            "Annualized risk metrics use 252 sessions per year and a 0% risk-free/target return. "
            "Less than a year of daily observations is available."
        )
    if count >= 2:
        mean = sum(returns, ZERO) / count
        deviation = (sum(((value - mean) ** 2 for value in returns), ZERO) / (count - 1)).sqrt()
        downside = (sum((min(value, ZERO) ** 2 for value in returns), ZERO) / count).sqrt()
        scale = TRADING_DAYS.sqrt()
        _set(result, "volatility", deviation * scale * HUNDRED)
        _set(
            result,
            "sharpe",
            mean / deviation * scale if deviation else None,
            "Daily return volatility is zero; Sharpe is undefined.",
        )
        _set(
            result,
            "sortino",
            mean / downside * scale if downside else None,
            "No negative daily returns; Sortino is undefined.",
        )
    else:
        for key in ("sharpe", "sortino", "volatility"):
            _set(result, key, None, "At least two completed daily returns are required.")
    elapsed = timestamp(result["coverage"]["end"]) - timestamp(result["coverage"]["start"])
    days = Decimal(str(elapsed.total_seconds())) / Decimal(86400)
    if days < 365:
        for key in ("cagr", "calmar"):
            _set(result, key, None, "At least 365 days of observed performance are required.")
        return
    cagr = wealth ** (YEAR_DAYS / days) - ONE
    _set(result, "cagr", cagr * HUNDRED)
    _set(result, "calmar", cagr / drawdown if drawdown else None, "No observed daily drawdown.")


def strategy_metrics(
    history: dict | None,
    trades: Any,
    window: Window,
    *,
    account: dict | None = None,
    activities: Any = (),
    as_of: datetime | str | None = None,
    history_as_of: datetime | str | None = None,
    history_complete: bool = True,
) -> dict:
    """Return JSON-safe metric values, definitions, coverage, and missing-data reasons.

    ``history`` must contain daily buckets fetched with ``analysis_window``;
    ``trades`` must be reconstructed across account history before filtering.
    ``history_as_of`` is the fetch time of this specific cached history response,
    so a live provisional daily bar cannot become final just as the clock advances.
    """
    result = {
        "metrics": {
            key: {
                "label": label,
                "group": group,
                "unit": unit,
                "definition": definition,
                "value": None,
                "status": "unavailable",
                "reason": "No completed trades in this period."
                if group == "trades"
                else "No completed daily returns are available in this period.",
            }
            for key, (label, group, unit, definition) in DEFINITIONS.items()
        },
        "coverage": {"used_days": 0, "start": None, "end": None},
        "notes": [
            "Account-wide daily equity includes open positions, dividends, and account costs. "
            "Returns approximate funding at period end; intraday funding timing is not measured.",
            "Daily metrics use completed NYSE sessions; trade metrics use gross completed "
            "lifecycles by exit time and exclude open positions and unallocated fees.",
        ],
    }
    _closed_trades(result, trades, window, history_complete)
    cutoff = timestamp(as_of or datetime.now(UTC))
    if history_as_of is not None:
        cutoff = min(cutoff, timestamp(history_as_of))
    cutoff = min(cutoff, window.end - timedelta(microseconds=1))
    try:
        returns = _daily_returns(
            result,
            history or {},
            window,
            account or {},
            activities,
            cutoff,
            history_complete,
        )
    except DateOutOfBounds:
        _unavailable(
            result, "equity", "Selected dates fall outside the supported exchange calendar."
        )
        return result
    if returns:
        _equity_metrics(result, returns)
    return result
