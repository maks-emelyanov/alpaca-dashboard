from datetime import UTC, datetime
from decimal import Decimal

import pytest

from alpaca_dashboard.models import Window
from alpaca_dashboard.performance import performance_series


def at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window(*, live=False, resolution="1Min"):
    return Window(
        at("2026-09-25T13:30:00Z"), at("2026-09-25T20:00:00Z"), resolution, "1D", live, "1D"
    )


def history(equity=(1010, 1520), cashflow=None):
    result = {
        "timestamp": [
            at("2026-09-25T14:00:00Z").timestamp(),
            at("2026-09-25T15:00:00Z").timestamp(),
        ],
        "equity": list(equity),
        "base_value": 1000,
        "base_value_asof": "2026-09-24",
    }
    if cashflow is not None:
        result["cashflow"] = cashflow
    return result


def test_cashflow_buckets_remove_deposits_but_preserve_dividends_and_fees():
    result = performance_series(
        history(cashflow={"CSD": [0, 500], "DIV": [0, 10], "FEE": [0, -1]}), window()
    )
    assert result["pnl"] == "20"
    assert result["pnl_pct"] == "2.00"
    assert result["error"] is None


def test_bucket_amounts_accumulate_and_withdrawals_are_signed():
    result = performance_series(
        history((1110, 1020), {"CSD": [100, 0], "CSW": [0, -100]}), window()
    )
    assert result["points"][0]["pnl"] == "10"
    assert result["pnl"] == "20"


def test_buckets_and_activities_do_not_double_count_transfer():
    transfer = {
        "id": "cash",
        "activity_type": "CSD",
        "net_amount": "500",
        "transaction_time": "2026-09-25T14:30:00Z",
    }
    result = performance_series(
        history(cashflow={"CSD": [0, 500]}),
        window(live=True),
        account={"equity": "1530"},
        activities=[transfer],
        as_of=at("2026-09-25T15:00:05Z"),
    )
    assert result["pnl"] == "30"
    assert result["error"] is None


def test_live_tail_adjusts_new_exactly_timed_funding():
    transfer = {
        "id": "cash",
        "activity_type": "CSD",
        "net_amount": "100",
        "transaction_time": "2026-09-25T15:00:02Z",
    }
    result = performance_series(
        history((1010, 1020), {}),
        window(live=True),
        account={"equity": "1130"},
        activities=[transfer],
        as_of=at("2026-09-25T15:00:05Z"),
    )
    assert result["pnl"] == "30"


def test_date_only_transfer_requires_bucket_coverage_for_live_tail():
    transfer = {"id": "cash", "activity_type": "CSD", "net_amount": "500", "date": "2026-09-25"}
    confirmed = performance_series(
        history(cashflow={"CSD": [0, 500]}),
        window(live=True),
        account={"equity": "1530"},
        activities=[transfer],
        as_of=at("2026-09-25T15:00:05Z"),
    )
    assert confirmed["pnl"] == "30"
    missing = performance_series(
        history((1010, 1020), {}),
        window(live=True),
        account={"equity": "1530"},
        activities=[transfer],
        as_of=at("2026-09-25T15:00:05Z"),
    )
    assert missing["pnl"] is None
    assert "New funding time" in missing["error"]


def test_missing_adjustment_data_is_explicitly_unavailable():
    result = performance_series(history(), window(), history_complete=False)
    assert result["pnl"] is None
    assert "incomplete" in result["error"]
    malformed = performance_series(history(cashflow={"CSD": [500]}), window())
    assert malformed["pnl"] is None
    assert "do not match" in malformed["error"]


def test_complete_activities_can_replace_cashflow_buckets():
    transfer = {
        "id": "cash",
        "activity_type": "CSD",
        "net_amount": "500",
        "transaction_time": "2026-09-25T14:30:00Z",
    }
    result = performance_series(history(), window(), activities=[transfer, transfer])
    assert result["pnl"] == "20"


def test_date_only_funding_without_buckets_does_not_invent_intraday_return():
    result = performance_series(
        history(),
        window(),
        activities=[
            {
                "id": "cash",
                "activity_type": "CSD",
                "net_amount": "500",
                "date": "2026-09-25",
            }
        ],
    )
    assert result["pnl"] is None
    assert "Funding time" in result["error"]


@pytest.mark.parametrize("later_equity, expected_pnl", [(1000, "0"), (1020, "20")])
def test_newly_funded_account_starts_at_first_nonzero_value(later_equity, expected_pnl):
    raw = history((0, 1000, later_equity), {"CSD": [0, 1000, 0]})
    raw["timestamp"].append(at("2026-09-25T16:00:00Z").timestamp())
    raw.pop("base_value_asof")
    result = performance_series(raw, window())
    assert result["points"][0]["pnl"] is None
    assert result["points"][0]["pnl_pct"] is None
    assert result["points"][1]["pnl"] == "0"
    assert result["pnl"] == expected_pnl
    assert Decimal(result["pnl_pct"]) == Decimal(expected_pnl) / 10
    assert result["error"] is None
    assert result["note"] == (
        "P&L starts at the first recorded nonzero balance (Sep 25, 2026 · 11:00 AM ET). "
        "Earlier zero-balance history is excluded."
    )

    # The historical note must not hide genuinely incomplete funding adjustments.
    raw["cashflow"]["CSD"].pop()
    incomplete = performance_series(raw, window())
    assert incomplete["note"] == result["note"]
    assert "do not match" in incomplete["error"]
    assert incomplete["pnl"] is None


def test_zero_starting_equity_has_no_percentage():
    raw = history((0, 0), {})
    raw.update(base_value=0)
    result = performance_series(raw, window())
    assert result["pnl"] == "0"
    assert result["pnl_pct"] is None
    assert result["note"] is None


def test_security_transfers_require_value_adjustments():
    result = performance_series(
        history(cashflow={}),
        window(),
        activities=[
            {
                "id": "stock",
                "activity_type": "ACATS",
                "date": "2026-09-25",
                "symbol": "AAPL",
            }
        ],
    )
    assert result["pnl"] is None
    assert result["error"]


def test_historical_window_never_appends_current_equity():
    result = performance_series(
        history(cashflow={}), window(), account={"equity": "999999"}, as_of=datetime.now(UTC)
    )
    assert len(result["points"]) == 2
    assert result["pnl"] == "520"


def test_empty_history_is_unavailable():
    result = performance_series({}, window())
    assert result["points"] == []
    assert result["pnl"] is None


def test_late_booked_timed_transfer_before_cached_timestamp_does_not_become_profit():
    transfer = {
        "id": "late",
        "activity_type": "CSD",
        "net_amount": "100",
        "transaction_time": "2026-09-25T14:59:59Z",
    }
    result = performance_series(
        history((1000, 1000), {}),
        window(live=True),
        account={"equity": "1100"},
        activities=[transfer],
        as_of=at("2026-09-25T15:00:20Z"),
    )
    assert result["pnl"] is None
    assert result["pnl_pct"] is None
    assert "Funding ledger and portfolio history disagree" in result["error"]
    refreshed = performance_series(
        history((1000, 1100), {"CSD": [0, 100]}),
        window(live=True),
        account={"equity": "1100"},
        activities=[transfer],
        as_of=at("2026-09-25T15:00:20Z"),
    )
    assert refreshed["pnl"] == "0"
    assert refreshed["error"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("timestamp", "malformed"),
        ("timestamp", True),
        ("equity", None),
        ("equity", "NaN"),
        ("equity", "bad"),
    ],
)
def test_invalid_history_point_is_explicitly_incomplete(field, value):
    raw = history((1000, 1100), {})
    raw[field][-1] = value
    result = performance_series(
        raw, window(live=True), account={"equity": "1100"}, as_of=at("2026-09-25T15:00:20Z")
    )
    assert result["pnl"] is None
    assert "Portfolio equity history is incomplete" in result["error"]
    assert all(point["pnl"] is None for point in result["points"])


def test_nonmonotonic_history_cannot_misalign_cashflow_buckets():
    raw = history((1000, 1100), {"CSD": [0, 100]})
    raw["timestamp"].reverse()
    result = performance_series(raw, window())
    assert result["pnl"] is None
    assert "incomplete" in result["error"]


def baseline_seed_history(*, live=False):
    """A daily snapshot can already include funding before account creation."""
    created = at("2026-06-11T02:27:00Z")
    raw = {
        "timestamp": [
            at("2026-06-11T00:00:00Z").timestamp(),
            at("2026-06-12T00:00:00Z").timestamp(),
            at("2026-06-13T00:00:00Z").timestamp(),
        ],
        "equity": [10000, 10012, "10042.85"],
        "base_value": 10000,
        "base_value_asof": "2026-06-09",
        "cashflow": {"JNLC": [10000, 0, 0]},
    }
    selected = Window(created, at("2026-06-14T00:00:00Z"), "1D", "ALL", live, "ALL")
    account = {"created_at": created.isoformat(), "equity": "10047.85"}
    seed = {
        "id": "initial-paper-funding",
        "activity_type": "JNLC",
        "net_amount": "10000",
        "date": "2026-06-10",
    }
    return raw, selected, account, seed


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("later_funding", [False, True])
def test_seed_in_baseline_bucket_matches_broader_period(live, later_funding):
    raw, selected, account, seed = baseline_seed_history(live=live)
    activities = [seed]
    if later_funding:
        raw["cashflow"].update(CSD=[0, 500, 0], CSW=[0, 0, -100])
        raw["equity"][1:] = [10512, "10442.85"]
        account["equity"] = "10447.85"
        activities.extend(
            [
                {
                    "id": "later-deposit",
                    "activity_type": "CSD",
                    "net_amount": "500",
                    "transaction_time": "2026-06-11T19:00:00Z",
                },
                {
                    "id": "later-withdrawal",
                    "activity_type": "CSW",
                    "net_amount": "-100",
                    "transaction_time": "2026-06-12T19:00:00Z",
                },
            ]
        )
    broader = Window(at("2026-01-01T05:00:00Z"), selected.end, "1D", "YTD", live, "YTD")
    results = [
        performance_series(
            raw,
            period,
            account=account,
            activities=activities,
            as_of=at("2026-06-13T14:00:00Z"),
        )
        for period in (selected, broader)
    ]
    for result in results:
        assert result["error"] is None
        assert Decimal(result["points"][0]["pnl"]) == 12
        assert Decimal(result["pnl"]) == Decimal("47.85" if live else "42.85")
        assert Decimal(result["pnl_pct"]) == Decimal("0.4785" if live else "0.4285")
        assert len(result["points"]) == (3 if live else 2)
    assert results[0]["points"] == results[1]["points"]


@pytest.mark.parametrize(
    "missing", ["activity", "bucket", "matching_amount", "unique_activity", "unique_bucket"]
)
def test_seed_in_baseline_bucket_requires_unambiguous_confirmation(missing):
    raw, selected, account, seed = baseline_seed_history()
    activities = [seed]
    if missing == "activity":
        activities = []
    elif missing == "bucket":
        raw["cashflow"] = {}
    elif missing == "matching_amount":
        seed["net_amount"] = "5000"
        raw["cashflow"]["JNLC"][0] = 5000
    elif missing == "unique_activity":
        activities.append({**seed, "id": "ambiguous-second-funding"})
    else:
        raw["timestamp"][1] = at("2026-06-11T03:00:00Z").timestamp()
        raw["cashflow"]["JNLC"][1] = 10000
    result = performance_series(
        raw,
        selected,
        account=account,
        activities=activities,
        as_of=at("2026-06-13T14:00:00Z"),
    )
    assert result["pnl"] is None
    assert result["pnl_pct"] is None
    assert result["error"] == "Initial funding baseline cannot be reconciled with portfolio history"


def late_seed_history(resolution, *, live=False, first_funded_equity=10000):
    """Paper equity can include its seed before the funding ledger books it."""
    created = at("2026-09-28T19:28:08Z")
    seed = {
        "id": "initial-paper-funding",
        "activity_type": "JNLC",
        "net_amount": "10000",
        "date": "2026-09-28",
        "created_at": "2026-09-28T19:33:00Z",
    }
    if resolution == "1D":
        stamps = [
            "2026-09-25T00:00:00Z",
            "2026-09-26T00:00:00Z",
            "2026-09-29T00:00:00Z",
            "2026-09-30T00:00:00Z",
        ]
        equities = [0, 10000, first_funded_equity, "10042.85"]
        start = at("2026-09-01T04:00:00Z")
    else:
        stamps = [
            "2026-09-28T19:25:00Z",
            "2026-09-28T19:30:00Z",
            "2026-09-28T19:35:00Z",
            "2026-09-28T19:40:00Z",
        ]
        equities = [10000, 10000, first_funded_equity, "10042.85"]
        start = created
    raw = {
        "timestamp": [at(value).timestamp() for value in stamps],
        "equity": equities,
        "profit_loss": [0, 0, first_funded_equity - 10000, "42.85"],
        "base_value": 10000,
        "base_value_asof": "2026-09-25",
        "cashflow": {"JNLC": [0, 0, 10000, 0]},
    }
    selected = Window(start, at("2026-10-01T00:00:00Z"), resolution, "ALL", live, "ALL")
    account = {"created_at": created.isoformat(), "equity": "10042.85"}
    return raw, selected, account, seed


@pytest.mark.parametrize("resolution", ["1D", "5Min"])
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("first_funded_equity", [10000, 10012])
def test_paper_seed_booked_after_initial_equity_is_not_a_loss(
    resolution, live, first_funded_equity
):
    raw, selected, account, seed = late_seed_history(
        resolution, live=live, first_funded_equity=first_funded_equity
    )
    result = performance_series(
        raw,
        selected,
        account=account,
        activities=[seed],
        as_of=at("2026-09-30T14:00:00Z"),
    )
    assert Decimal(result["pnl"]) == Decimal("42.85")
    assert Decimal(result["pnl_pct"]) == Decimal("0.4285")
    assert result["error"] is None
    assert all(at(point["timestamp"]) >= at(account["created_at"]) for point in result["points"])
    seed_point = next(
        point
        for point in result["points"]
        if at(point["timestamp"]).timestamp() == raw["timestamp"][2]
    )
    assert Decimal(seed_point["pnl"]) == first_funded_equity - 10000


@pytest.mark.parametrize("deposit_bucket", [2, 3])
@pytest.mark.parametrize("live", [False, True])
def test_seed_correction_preserves_subsequent_deposit_and_withdrawal(deposit_bucket, live):
    raw, selected, account, seed = late_seed_history("5Min", live=live, first_funded_equity=10012)
    raw["cashflow"].update(CSD=[0, 0, 0, 0], CSW=[0, 0, 0, -100])
    raw["cashflow"]["CSD"][deposit_bucket] = 500
    if deposit_bucket == 2:
        raw["equity"][2] = "10512"
    raw["equity"][-1] = account["equity"] = "10442.85"
    deposit = {
        "id": "later-deposit",
        "activity_type": "CSD",
        "net_amount": "500",
        "transaction_time": "2026-09-28T19:34:00Z"
        if deposit_bucket == 2
        else "2026-09-28T19:37:00Z",
    }
    withdrawal = {
        "id": "later-withdrawal",
        "activity_type": "CSW",
        "net_amount": "-100",
        "transaction_time": "2026-09-28T19:38:00Z",
    }
    result = performance_series(
        raw,
        selected,
        account=account,
        activities=[seed, deposit, withdrawal],
        as_of=at("2026-09-28T19:40:05Z"),
    )
    assert Decimal(result["pnl"]) == Decimal("42.85")
    assert Decimal(result["pnl_pct"]) == Decimal("0.4285")
    seed_point = next(
        point for point in result["points"] if point["timestamp"] == "2026-09-28T19:35:00+00:00"
    )
    assert Decimal(seed_point["pnl"]) == 12
    assert result["error"] is None


def test_period_after_account_creation_still_adjusts_new_funding():
    _, _, account, seed = late_seed_history("5Min")
    selected = Window(
        at("2026-09-29T13:30:00Z"),
        at("2026-09-29T20:00:00Z"),
        "1Min",
        "1D",
        True,
        "1D",
    )
    raw = {
        "timestamp": [
            at("2026-09-29T14:00:00Z").timestamp(),
            at("2026-09-29T15:00:00Z").timestamp(),
        ],
        "equity": ["10552.85", "10462.85"],
        "base_value": "10042.85",
        "base_value_asof": "2026-09-28",
        "cashflow": {"CSD": [500, 0], "CSW": [0, -100]},
    }
    account["equity"] = "10462.85"
    activities = [
        seed,
        {
            "id": "next-day-deposit",
            "activity_type": "CSD",
            "net_amount": "500",
            "transaction_time": "2026-09-29T13:45:00Z",
        },
        {
            "id": "next-day-withdrawal",
            "activity_type": "CSW",
            "net_amount": "-100",
            "transaction_time": "2026-09-29T14:45:00Z",
        },
    ]
    result = performance_series(
        raw, selected, account=account, activities=activities, as_of=at("2026-09-29T15:00:05Z")
    )
    assert Decimal(result["pnl"]) == 20
    assert Decimal(result["pnl_pct"]) == Decimal(20) / Decimal("10042.85") * 100
    assert result["error"] is None


@pytest.mark.parametrize("missing", ["activity", "bucket", "matching_amount", "unique_activity"])
def test_prefunded_equity_without_confirmed_initial_seed_is_unavailable(missing):
    raw, selected, account, seed = late_seed_history("1D")
    activities = [seed]
    if missing == "activity":
        activities = []
    elif missing == "bucket":
        raw["cashflow"] = {}
    elif missing == "matching_amount":
        seed["net_amount"] = "5000"
        raw["cashflow"]["JNLC"][2] = 5000
    else:
        activities.append({**seed, "id": "ambiguous-second-funding"})
    result = performance_series(
        raw,
        selected,
        account=account,
        activities=activities,
        as_of=at("2026-09-30T14:00:00Z"),
    )
    assert result["pnl"] is None
    assert result["pnl_pct"] is None
    assert result["error"]
