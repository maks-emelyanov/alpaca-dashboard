from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import sqrt

import pytest

from alpaca_dashboard.analytics import analysis_window, strategy_metrics
from alpaca_dashboard.models import Window
from alpaca_dashboard.timeframes import _calendar


def at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window(start="2026-09-21T04:00:00Z", end="2026-09-23T04:00:00Z"):
    return Window(at(start), at(end), "15Min", "CUSTOM:test", False, "CUSTOM")


def history(values=(100, 210, 189), flows=(0, 100, 0), days=("18", "21", "22")):
    return {
        "timestamp": [at(f"2026-09-{day}T04:00:00Z").timestamp() for day in days],
        "equity": list(values),
        "cashflow": {"CSD": list(flows)},
        "base_value": values[0],
        "base_value_asof": "2026-09-17",
    }


def metrics(raw=None, trades=(), selected=None, **kwargs):
    return strategy_metrics(
        raw if raw is not None else history(),
        trades,
        selected or window(),
        as_of=kwargs.pop("as_of", at("2026-09-25T22:00:00Z")),
        **kwargs,
    )


def value(result, key):
    raw = result["metrics"][key]["value"]
    return Decimal(raw) if raw is not None else None


def trade(pnl, *, closed="2026-09-21T16:00:00Z", status="Completed", duration=3600):
    return {
        "closed_at": closed,
        "status": status,
        "realized_pnl": str(pnl),
        "duration_seconds": duration,
    }


def test_funding_adjusted_returns_compound_using_actual_prior_equity():
    result = metrics()
    # Day one +10% after removing deposit; day two -10% on the funded $210.
    # Arithmetic P&L / initial equity is -11%, which is not linked return.
    assert value(result, "total_return") == -1
    assert value(result, "max_drawdown") == 10
    assert value(result, "sharpe") == 0
    assert value(result, "sortino") == 0
    assert float(value(result, "volatility")) == pytest.approx(sqrt(0.02) * sqrt(252) * 100)
    assert result["coverage"]["used_days"] == 2
    assert result["coverage"]["start"] == "2026-09-18T20:00:00+00:00"
    assert result["coverage"]["end"] == "2026-09-22T20:00:00+00:00"
    assert "estimate" in result["metrics"]["total_return"]["definition"]


def test_withdrawals_are_not_losses_and_trade_breakevens_are_in_win_rate():
    result = metrics(
        history((100, 60, 54), (0, -50, 0)),
        trades=[trade(100), trade(-50), trade(0)],
    )
    assert value(result, "total_return") == -1
    assert value(result, "trade_count") == 3
    assert float(value(result, "win_rate")) == pytest.approx(100 / 3)
    assert value(result, "profit_factor") == 2
    assert value(result, "payoff_ratio") == 2
    assert value(result, "average_win") == 100
    assert value(result, "average_loss") == -50
    assert value(result, "gross_pnl") == 50
    assert value(result, "expectancy") == Decimal(50) / 3
    assert value(result, "average_duration") == 3600
    assert result["coverage"]["wins"] == result["coverage"]["losses"] == 1
    assert result["coverage"]["breakeven"] == 1


def test_trade_exit_filter_is_half_open_and_excludes_incomplete_records():
    result = metrics(
        trades=[
            trade(100, closed="2026-09-21T04:00:00Z"),
            trade(1000, closed="2026-09-23T04:00:00Z"),
            trade(-1000, closed="2026-09-20T23:59:00Z"),
            trade(1000, status="Incomplete: execution history"),
            trade("NaN"),
            trade(500, closed=None, status="Incomplete: malformed execution record"),
        ]
    )
    assert value(result, "trade_count") == 1
    assert value(result, "gross_pnl") == 100
    assert value(result, "win_rate") == 100
    assert result["coverage"]["excluded"] == 2
    assert result["coverage"]["undated"] == 1
    assert any("no usable exit time" in note for note in result["notes"])


@pytest.mark.parametrize("pnl", [100, 0])
def test_no_losses_produces_unavailable_profit_factor_instead_of_infinity(pnl):
    result = metrics(trades=[trade(pnl)])
    assert value(result, "profit_factor") is None
    assert "No losing" in result["metrics"]["profit_factor"]["reason"]
    assert value(result, "payoff_ratio") is None


def test_no_trades_have_no_win_rate_or_expectancy():
    result = metrics()
    assert value(result, "trade_count") == 0
    assert value(result, "win_rate") is None
    assert value(result, "expectancy") is None


def test_incomplete_execution_ledger_cannot_claim_a_complete_trade_sample():
    result = metrics(trades=[trade(100)], history_complete=False)
    assert value(result, "win_rate") is None
    assert value(result, "trade_count") is None
    assert "incomplete" in result["metrics"]["win_rate"]["reason"]
    # Valid broker funding buckets still establish independent equity returns.
    assert value(result, "total_return") == -1


def test_daily_left_labels_and_end_labels_represent_the_same_completed_sessions():
    raw = history()
    result = metrics(raw)
    raw["timestamp"] = [stamp + 20 * 3600 for stamp in raw["timestamp"]]
    assert metrics(raw)["metrics"] == result["metrics"]


def test_current_and_cached_preclose_bars_are_not_treated_as_full_days():
    raw = history((100, 110, 9999), (0, 0, 0))
    selected = window(end="2026-09-22T23:00:00Z")
    result = metrics(
        raw,
        selected=selected,
        as_of=at("2026-09-22T21:00:00Z"),
        history_as_of=at("2026-09-22T19:59:00Z"),
    )
    assert value(result, "total_return") == 10
    assert result["coverage"]["used_days"] == 1
    assert result["coverage"]["end"] == "2026-09-21T20:00:00+00:00"
    assert value(result, "sharpe") is None


def test_single_session_range_retains_previous_close_return_after_market_close():
    selected = window("2026-09-21T13:30:00Z", "2026-09-21T20:00:00.000001Z")
    result = metrics(selected=selected)
    assert value(result, "total_return") == 10
    assert value(result, "max_drawdown") == 0
    assert value(result, "sharpe") is None
    assert "two completed" in result["metrics"]["sharpe"]["reason"]


def test_rolling_midday_start_excludes_the_partial_first_session():
    result = metrics(selected=window("2026-09-21T17:00:00Z"))
    assert value(result, "total_return") == -10
    assert result["coverage"]["start"] == "2026-09-21T20:00:00+00:00"
    assert result["coverage"]["used_days"] == 1


def test_broker_base_value_can_anchor_first_day_without_a_prior_raw_bucket():
    raw = history((110, 99), (0, 0), ("21", "22"))
    raw.update(base_value=100, base_value_asof="2026-09-18")
    assert value(metrics(raw), "total_return") == -1


@pytest.mark.parametrize("kind", ["middle", "tail"])
def test_missing_sessions_cannot_be_annualized_or_hide_drawdowns(kind):
    raw = history((100, 110, 121), (0, 0, 0), ("18", "21", "23"))
    if kind == "tail":
        raw = history((100, 110), (0, 0), ("18", "21"))
    result = metrics(raw, selected=window(end="2026-09-24T04:00:00Z"))
    for key in ("total_return", "max_drawdown", "sharpe", "cagr"):
        assert value(result, key) is None
        assert "missing a required session" in result["metrics"][key]["reason"]


def test_missing_leading_sessions_cannot_be_mistaken_for_an_initial_partial_session():
    raw = history((100, 110), (0, 0), ("23", "24"))
    raw.pop("base_value_asof")
    result = metrics(
        raw,
        selected=window(end="2026-09-25T04:00:00Z"),
        account={"created_at": "2020-01-01T12:00:00Z"},
    )
    assert value(result, "total_return") is None
    assert "missing a required session" in result["metrics"]["total_return"]["reason"]


def test_old_account_without_starting_close_does_not_silently_shorten_the_range():
    raw = history((100, 110), (0, 0), ("21", "22"))
    raw.pop("base_value_asof")
    result = metrics(raw, account={"created_at": "2020-01-01T12:00:00Z"})
    assert value(result, "total_return") is None
    assert "starting session close" in result["metrics"]["total_return"]["reason"]


def test_zero_volatility_and_no_downside_do_not_produce_infinite_ratios():
    result = metrics(history((100, 110, 121), (0, 0, 0)))
    assert value(result, "total_return") == 21
    assert value(result, "volatility") == 0
    assert value(result, "sharpe") is None
    assert value(result, "sortino") is None
    assert "zero" in result["metrics"]["sharpe"]["reason"]
    assert "No negative" in result["metrics"]["sortino"]["reason"]


def test_sortino_uses_all_days_in_downside_denominator_and_sharpe_sample_deviation():
    result = metrics(history((100, 120, 108), (0, 0, 0)))
    assert float(value(result, "sharpe")) == pytest.approx(0.05 / sqrt(0.045) * sqrt(252))
    assert float(value(result, "sortino")) == pytest.approx(0.05 / sqrt(0.005) * sqrt(252))


def test_short_history_does_not_extrapolate_cagr():
    result = metrics()
    assert value(result, "cagr") is None
    assert value(result, "calmar") is None
    assert "365 days" in result["metrics"]["cagr"]["reason"]


def test_year_long_cagr_uses_calendar_elapsed_time_and_calmar_uses_drawdown():
    first, last = at("2025-09-19T20:00:00Z"), at("2026-09-21T20:00:00Z")
    day, stamps = first.date(), []
    while day <= last.date():
        session = _calendar().session(day)
        if session:
            stamps.append(session.close.timestamp())
        day += timedelta(days=1)
    equities = [100, 90] + [110] * (len(stamps) - 2)
    raw = {"timestamp": stamps, "equity": equities, "cashflow": {}}
    result = metrics(
        raw,
        selected=window("2025-09-20T04:00:00Z", "2026-09-22T04:00:00Z"),
    )
    expected = (1.1 ** (365.25 / (last - first).days) - 1) * 100
    assert float(value(result, "cagr")) == pytest.approx(expected)
    assert value(result, "max_drawdown") == 10
    assert float(value(result, "calmar")) == pytest.approx(expected / 10)


@pytest.mark.parametrize("broken", ["equity", "cashflow", "security", "non_session", "duplicate"])
def test_unreliable_equity_or_funding_yields_explicit_unavailable_metrics(broken):
    raw, activities = history(), []
    if broken == "equity":
        raw["equity"][1] = "NaN"
    elif broken == "cashflow":
        raw["cashflow"]["CSD"].pop()
    elif broken == "security":
        activities = [{"id": "transfer", "activity_type": "ACATS", "date": "2026-09-21"}]
    elif broken == "non_session":
        raw["timestamp"][1] = at("2026-09-20T14:00:00Z").timestamp()
    else:
        raw["timestamp"][1] = raw["timestamp"][0]
    result = metrics(raw, activities=activities, trades=[trade(100)])
    assert value(result, "total_return") is None
    assert result["metrics"]["total_return"]["reason"]
    assert value(result, "win_rate") == 100


def test_zero_starting_capital_establishes_a_funded_anchor_without_a_deposit_return():
    result = metrics(history((0, 100, 110), (0, 100, 0)))
    assert value(result, "total_return") == 10
    assert result["coverage"]["used_days"] == 1


def test_complete_loss_is_not_annualized_or_silently_skipped():
    result = metrics(history((100, 0, 100), (0, 0, 0)))
    assert value(result, "total_return") is None
    assert "positive wealth" in result["metrics"]["total_return"]["reason"]


def test_late_posted_initial_funding_does_not_create_prefunding_observation_time():
    raw = {
        "timestamp": [
            at(day).timestamp()
            for day in (
                "2026-09-25T00:00:00Z",
                "2026-09-26T00:00:00Z",
                "2026-09-29T00:00:00Z",
                "2026-09-30T00:00:00Z",
            )
        ],
        "equity": [0, 10000, 10012, "10042.85"],
        "base_value": 10000,
        "base_value_asof": "2026-09-25",
        "cashflow": {"JNLC": [0, 0, 10000, 0]},
    }
    created = "2026-09-28T19:28:08Z"
    result = metrics(
        raw,
        selected=window(created, "2026-09-30T04:00:00Z"),
        account={"created_at": created},
        as_of=at("2026-09-30T01:00:00Z"),
        activities=[
            {
                "id": "seed",
                "activity_type": "JNLC",
                "net_amount": "10000",
                "date": "2026-09-28",
            }
        ],
    )
    assert value(result, "total_return") == pytest.approx(Decimal("30.85") / 10012 * 100)
    assert result["coverage"]["used_days"] == 1
    assert result["coverage"]["start"] == "2026-09-28T20:00:00+00:00"


def test_analysis_window_fetches_previous_exchange_session_across_holiday():
    selected = window("2026-09-08T04:00:00Z", "2026-09-10T04:00:00Z")
    fetch = analysis_window(selected)
    # Monday September 7 is Labor Day; Friday is the preceding NYSE session.
    assert fetch.start == datetime(2026, 9, 4, 4, tzinfo=UTC)
    assert fetch.end == selected.end
    assert fetch.resolution == "1D"
    assert fetch.key == f"analytics:{selected.key}"


def test_custom_dates_outside_calendar_bounds_are_gracefully_unavailable():
    selected = window("1980-01-01T05:00:00Z", "1980-01-05T05:00:00Z")
    fetch = analysis_window(selected)
    assert fetch.resolution == "1D"
    assert fetch.start == selected.start
    result = metrics(selected=selected)
    assert value(result, "total_return") is None
    assert result["metrics"]["total_return"]["reason"]


def test_early_close_is_completed_at_its_calendar_close():
    raw = {
        "timestamp": [
            at("2026-11-25T05:00:00Z").timestamp(),
            at("2026-11-27T05:00:00Z").timestamp(),
        ],
        "equity": [100, 110],
        "cashflow": {},
    }
    result = metrics(
        raw,
        selected=window("2026-11-27T14:30:00Z", "2026-11-27T18:00:00.000001Z"),
        as_of=at("2026-11-27T18:01:00Z"),
    )
    assert value(result, "total_return") == 10
    assert result["coverage"]["end"] == "2026-11-27T18:00:00+00:00"
