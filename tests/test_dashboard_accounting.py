from decimal import Decimal

import pytest

from alpaca_dashboard.accounting import order_rows, position_rows, trade_rows


def fill(identity, side, qty, price, minute, symbol="AAPL", **extra):
    return {
        "id": identity,
        "activity_type": "FILL",
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": f"2026-09-25T14:{minute:02}:00Z",
        **extra,
    }


def test_scaling_partial_exits_and_duplicate_pages_use_execution_quantity():
    entries = [
        fill("a", "buy", ".5", "10", 0, cum_qty=".5"),
        fill("b", "buy", ".5", "12", 1, cum_qty="1"),
        fill("c", "sell", ".25", "14", 2),
        fill("d", "buy", ".25", "8", 3),
        fill("e", "sell", "1", "15", 4),
    ]
    rows = trade_rows(entries + [entries[1]], [], [])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "Completed"
    assert Decimal(row["quantity"]) == Decimal("1.25")
    assert Decimal(row["entry_price"]) == Decimal("10.4")
    assert Decimal(row["exit_price"]) == Decimal("14.8")
    assert Decimal(row["realized_pnl"]) == Decimal("5.50")
    assert len(row["details"]) == 5
    assert row["duration_seconds"] == 240


def test_short_trade_percent_uses_entry_notional():
    (row,) = trade_rows([fill("a", "sell", "2", "100", 0), fill("b", "buy", "2", "90", 1)], [], [])
    assert row["direction"] == "short"
    assert Decimal(row["realized_pnl"]) == 20
    assert Decimal(row["realized_pct"]) == 10


def test_reversal_allocates_fill_between_two_lifecycles():
    rows = trade_rows(
        [
            fill("a", "buy", "2", "10", 0),
            fill("b", "sell", "3", "12", 1),
            fill("c", "buy", "1", "11", 2),
        ],
        [],
        [],
    )
    assert len(rows) == 2
    short, long = rows
    assert long["direction"] == "long"
    assert Decimal(long["realized_pnl"]) == 4
    assert Decimal(short["realized_pnl"]) == 1
    assert long["details"][-1]["qty"] == "2"
    assert short["details"][0]["qty"] == "1"
    assert long["details"][-1]["original_qty"] == "3"
    assert short["id"] != long["id"]


def test_valid_open_lifecycle_is_only_shown_in_open_positions():
    fills = [fill("a", "buy", "2", "10", 0)]
    assert trade_rows(fills, [{"symbol": "AAPL", "qty": "2", "side": "long"}], []) == []


@pytest.mark.parametrize(
    "activities,positions,complete",
    [
        ([fill("a", "sell", 2, 10, 0)], [], True),
        ([fill("a", "buy", 2, 10, 0), fill("b", "sell", 2, 11, 1)], [], False),
        (
            [
                fill("a", "buy", 2, 10, 0),
                fill("b", "sell", 2, 11, 1),
                {"id": "split", "activity_type": "SSP", "symbol": "AAPL"},
            ],
            [],
            True,
        ),
    ],
)
def test_incomplete_history_does_not_invent_returns(activities, positions, complete):
    rows = trade_rows(activities, positions, [], history_complete=complete)
    assert rows
    assert all(row["realized_pnl"] is None for row in rows)
    assert all(row["status"].startswith("Incomplete") for row in rows)


def test_unsupported_instrument_keeps_execution_detail():
    (row,) = trade_rows(
        [
            fill("a", "buy", 1, 10, 0, symbol="AAPL261016C00150000"),
            fill("b", "sell", 1, 11, 1, symbol="AAPL261016C00150000"),
        ],
        [],
        [],
    )
    assert row["realized_pnl"] is None
    assert len(row["details"]) == 2
    assert "Unsupported" in row["status"]


def test_full_lifecycle_not_rebuilt_at_timeframe_boundary():
    rows = trade_rows(
        [
            {**fill("a", "buy", 1, 10, 0), "transaction_time": "2026-08-01T14:00:00Z"},
            fill("b", "sell", 1, 11, 0),
        ],
        [],
        [],
    )
    assert Decimal(rows[0]["realized_pnl"]) == 1
    assert rows[0]["opened_at"].startswith("2026-08")


def test_bracket_relationships_and_broker_only_execution_details():
    parent = {
        "id": "entry",
        "symbol": "AAPL",
        "side": "buy",
        "status": "filled",
        "legs": [
            {
                "id": "stop",
                "symbol": "AAPL",
                "side": "sell",
                "status": "new",
                "type": "stop",
                "stop_price": "9",
            }
        ],
    }
    orders = order_rows([parent, parent["legs"][0]])
    stop = next(row for row in orders if row["id"] == "stop")
    assert stop["parent_id"] == "entry"
    assert "attribution" not in stop
    assert "metadata" not in stop
    (row,) = trade_rows(
        [fill("a", "buy", 1, 10, 0, order_id="entry"), fill("b", "sell", 1, 9, 1, order_id="stop")],
        [],
        [parent],
    )
    assert row["details"][0]["order_id"] == "entry"
    assert row["details"][1]["order_id"] == "stop"
    assert Decimal(row["realized_pnl"]) == -1
    assert "attribution" not in row
    assert "metadata" not in row
    (pos,) = position_rows(
        [
            {
                "symbol": "AAPL",
                "qty": "1",
                "side": "long",
                "avg_entry_price": "10",
                "unrealized_plpc": ".05",
            }
        ],
        [parent],
    )
    assert "attribution" not in pos
    assert "metadata" not in pos
    assert pos["linked_orders"] == ["stop"]
    assert pos["stop_price"] == "9"
    assert pos["unrealized_pct"] == "5.00"
    assert pos["avg_entry_price"] == "10"


def test_malformed_fill_is_visible_without_crashing():
    (row,) = trade_rows([fill("a", "buy", "bad", 10, 0)], [], [])
    assert "malformed" in row["status"]
    assert row["realized_pnl"] is None


def test_alpaca_sell_short_activity_round_trip_preserves_actual_side():
    activities = [
        fill(
            "short-a",
            "sell_short",
            "1.25",
            "123.40",
            0,
            order_status="partially_filled",
            cum_qty="1.25",
        ),
        fill("short-b", "sell_short", ".75", "124.00", 1, order_status="filled", cum_qty="2"),
        fill("cover", "buy", "2", "122.00", 2, order_status="filled"),
    ]
    (row,) = trade_rows(activities, [], [])
    assert row["status"] == "Completed"
    assert row["direction"] == "short"
    assert Decimal(row["quantity"]) == 2
    assert Decimal(row["realized_pnl"]) == Decimal("3.2500")
    assert row["details"][0]["side"] == "sell_short"
    assert row["details"][0]["order_status"] == "partially_filled"


@pytest.mark.parametrize(
    "buy_side,sell_side", [("buy_minus", "sell_plus"), ("buy", "sell_short_exempt")]
)
def test_documented_directional_activity_variants(buy_side, sell_side):
    (row,) = trade_rows([fill("a", buy_side, 1, 10, 0), fill("b", sell_side, 1, 11, 1)], [], [])
    assert row["status"] == "Completed"
    assert Decimal(row["realized_pnl"]) == 1


@pytest.mark.parametrize("side", ["buy_to_cover", "cross", "undisclosed", "cross_short"])
def test_undocumented_or_ambiguous_directions_do_not_guess(side):
    (row,) = trade_rows([fill("a", side, 1, 10, 0)], [], [])
    assert row["realized_pnl"] is None
    assert row["status"].startswith("Incomplete")
