"""Range-state and presentation checks for optional Dash UI dependencies."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("dash")
pytest.importorskip("dash_ag_grid")

from alpaca_dashboard import ui  # noqa: E402
from alpaca_dashboard.models import Window  # noqa: E402

NOW = datetime(2026, 9, 30, 16, tzinfo=UTC)
SNAPSHOT = {"account": {"created_at": "2024-01-01T00:00:00Z"}}


def selected(preset="1D", start=None, end=None):
    return {"preset": preset, "start_date": start, "end_date": end}


def callback(app, output_id):
    item = next(value for key, value in app.callback_map.items() if f"{output_id}." in key)
    return item["callback"].__wrapped__


@pytest.mark.parametrize("source", ["chart", "history"])
def test_either_control_updates_both_when_linked(source):
    chart, history = selected(), selected()
    if source == "chart":
        chart = selected("1M")
    else:
        history = selected("1M")
    drafts, accepted, errors = ui.change_ranges(
        f"{source}-preset",
        chart,
        history,
        True,
        ui.initial_ranges(),
        SNAPSHOT,
        NOW,
    )
    assert accepted["chart"] == accepted["history"] == selected("1M")
    assert drafts["chart"] == drafts["history"]
    assert not any(errors.values())


def test_unlink_preserves_then_relink_chart_wins():
    accepted = {"chart": selected("1M"), "history": selected("1M"), "linked": True}
    drafts, result, _ = ui.change_ranges(
        "link-ranges",
        accepted["chart"],
        accepted["history"],
        False,
        accepted,
        SNAPSHOT,
        NOW,
    )
    assert result["chart"] == accepted["chart"]
    assert result["history"] == accepted["history"]
    drafts, result, _ = ui.change_ranges(
        "history-preset",
        drafts["chart"],
        selected("1W"),
        False,
        result,
        SNAPSHOT,
        NOW,
    )
    assert result["chart"]["preset"] == "1M"
    assert result["history"]["preset"] == "1W"
    drafts, result, _ = ui.change_ranges(
        "link-ranges",
        drafts["chart"],
        drafts["history"],
        True,
        result,
        SNAPSHOT,
        NOW,
    )
    assert result["history"] == result["chart"] == selected("1M")
    assert drafts["history"] == drafts["chart"]


@pytest.mark.parametrize(
    "custom",
    [
        selected("CUSTOM"),
        selected("CUSTOM", "2026-09-10"),
        selected("CUSTOM", "2026-09-20", "2026-09-10"),
        selected("CUSTOM", "2026-09-10", "2026-10-10"),
    ],
)
def test_invalid_custom_keeps_prior_view_and_draft_input(custom):
    accepted = ui.initial_ranges()
    drafts, result, errors = ui.change_ranges(
        "chart-dates",
        custom,
        selected(),
        True,
        accepted,
        SNAPSHOT,
        NOW,
    )
    assert result == accepted
    assert drafts["chart"] == custom
    assert errors["chart"]
    assert not errors["history"]


def test_custom_one_day_commits_both_controls():
    custom = selected("CUSTOM", "2026-09-20", "2026-09-20")
    drafts, result, errors = ui.change_ranges(
        "history-dates",
        selected(),
        custom,
        True,
        ui.initial_ranges(),
        SNAPSHOT,
        NOW,
    )
    assert result["history"] == result["chart"] == custom
    assert drafts["chart"] == custom
    assert not any(errors.values())


def test_date_filter_uses_exclusive_midnight_and_retains_incomplete():
    window = Window(NOW, NOW + timedelta(days=1), "1Min", "test", False, "CUSTOM")
    rows = [
        {"id": "before", "closed_at": (NOW - timedelta(seconds=1)).isoformat()},
        {"id": "start", "closed_at": NOW.isoformat()},
        {"id": "inside", "closed_at": (NOW + timedelta(hours=12)).isoformat()},
        {"id": "end", "closed_at": window.end.isoformat()},
        {"id": "missing", "closed_at": None, "status": "Incomplete: missing fills"},
    ]
    assert [row["id"] for row in ui.filter_rows(rows, "closed_at", window)] == [
        "start",
        "inside",
        "missing",
    ]


def test_numbers_converted_only_for_presentation():
    source = [{"id": "trade", "realized_pnl": "10.123", "quantity": "0.001", "entry_price": None}]
    result = ui.grid_rows(source)
    assert source[0]["realized_pnl"] == "10.123"
    assert result[0]["realized_pnl"] == 10.123
    assert result[0]["quantity"] == 0.001
    assert result[0]["entry_price"] is None


def test_chart_has_hover_data_shading_and_stable_revision():
    window = Window(NOW, NOW + timedelta(days=1), "1Min", "1D:2026-09-30", True, "1D")
    series = {
        "points": [
            {"timestamp": NOW.isoformat(), "equity": "1000", "pnl": "-2.1", "pnl_pct": "-0.21"}
        ],
        "pnl": "-2.1",
        "pnl_pct": "-0.21",
    }
    figure = ui.chart_figure(series, window)
    assert figure.layout.uirevision == window.key
    assert figure.layout.xaxis.showspikes
    assert figure.layout.hoverdistance == -1
    assert figure.data[0].line.color == ui.RED
    assert figure.data[0].fill == "tozeroy"
    assert figure.data[0].customdata[0][0] == "-2.1"
    assert "ET" in figure.data[0].customdata[0][2]
    assert figure.data[0].customdata[0][3] == "$1,000.00"


def test_layout_and_assets_are_served_without_broker():
    service = SimpleNamespace(refresh_seconds=5, snapshot=lambda: {}, history=lambda window: {})
    app = ui.create_app(service)
    client = app.server.test_client()
    response = client.get("/_dash-layout")
    assert response.status_code == 200
    layout = response.get_data(as_text=True)
    assert '"interval":5000' in layout
    assert '"getRowId":"params.data.id"' in layout
    assert "paginationPageSize" in layout
    assert "Alpaca Dashboard" in layout
    assert '"fileName":"alpaca-trades.csv"' in layout
    assert '"fileName":"alpaca-positions.csv"' in layout
    assert '"fileName":"alpaca-orders.csv"' in layout
    assert '"suppressContentVisibilityAuto":true' in layout
    assert '"headerName":"Strategy"' not in layout
    assert app.title == "Alpaca Dashboard · Account activity"
    assert client.get("/assets/dashboard.css").status_code == 200
    assert client.get("/assets/dashboard.js").status_code == 200


def test_tables_leave_current_holdings_visible_for_historical_ranges(monkeypatch):
    service = SimpleNamespace(refresh_seconds=5)
    app = ui.create_app(service)
    snapshot = {
        **SNAPSHOT,
        "positions": [
            {
                "asset_id": "AAPL",
                "symbol": "AAPL",
                "qty": "3",
                "side": "long",
                "avg_entry_price": "100",
                "current_price": "110",
            }
        ],
        "orders": [],
        "activities": [],
        "history_complete": True,
    }
    window = Window(
        NOW - timedelta(days=100), NOW - timedelta(days=90), "5Min", "old", False, "CUSTOM"
    )
    monkeypatch.setattr(ui, "selection_window", lambda *args: window)
    trades, positions, orders = callback(app, "trades-grid")(snapshot, ui.initial_ranges())
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"] == 3
    assert not orders
    assert isinstance(trades, list)


def test_stale_status_keeps_last_balances_and_labels_error():
    app = ui.create_app(SimpleNamespace(refresh_seconds=5))
    result = callback(app, "buying-power")(
        {
            "account": {"equity": "1020", "buying_power": "2000"},
            "as_of": NOW.isoformat(),
            "error": "Connection unavailable",
            "history_complete": True,
            "clock": {"is_open": True},
        }
    )
    assert result[0] == "$2,000.00"
    assert "Showing last available data" in result[2]
    assert "Connection unavailable" in result[2]
    assert result[-1] == "Market open"


def test_multiple_exit_order_prices_survive_presentation():
    rows = ui.grid_rows(
        [{"id": "position", "stop_price": "95.25, 96.50", "target_price": "110.00, 115.00"}]
    )
    assert rows[0]["stop_price"] == "95.25, 96.50"
    assert rows[0]["target_price"] == "110.00, 115.00"
    stop = next(column for column in ui.POSITION_COLUMNS if column["field"] == "stop_price")
    assert stop["valueFormatter"] == {"function": "dashboardPrices(params.value)"}


def test_selected_trade_shows_readable_fills_and_complete_execution_records():
    app = ui.create_app(SimpleNamespace(refresh_seconds=5))
    fill = {
        "id": "execution-1",
        "transaction_time": NOW.isoformat(),
        "side": "buy",
        "allocation": "entry",
        "qty": ".25",
        "price": "200",
        "order_id": "order-1",
    }
    details, style = callback(app, "trade-details")(
        [
            {
                "symbol": "AAPL",
                "status": "Completed",
                "details": [fill],
            }
        ],
        "trades",
    )
    assert style == {}
    rendered = str(details)
    assert "AAPL · Execution details" in rendered
    assert "order-1" in rendered
    assert "Complete execution records" in rendered
    assert "Completed" in rendered
    assert "execution-1" in rendered
    assert callback(app, "trade-details")([], "trades") == ([], {"display": "none"})


def test_explicit_zoom_payload_is_preserved_and_double_click_resets():
    window = Window(NOW, NOW + timedelta(days=1), "1Min", "day", True, "1D")
    figure = ui.chart_figure({"points": []}, window)
    ui.preserve_viewport(
        figure,
        {
            "xaxis.range[0]": "2026-09-30 10:00",
            "xaxis.range[1]": "2026-09-30 11:00",
            "yaxis.range": [-10, 20],
        },
    )
    assert figure.layout.xaxis.range == ("2026-09-30 10:00", "2026-09-30 11:00")
    assert figure.layout.xaxis.autorange is False
    assert figure.layout.yaxis.range == (-10, 20)
    ui.preserve_viewport(figure, {"xaxis.autorange": True, "yaxis.autorange": True})
    assert figure.layout.xaxis.autorange is True
    assert figure.layout.yaxis.autorange is True


def test_chart_range_change_clears_old_zoom_payload(monkeypatch):
    window = Window(NOW, NOW + timedelta(days=1), "1Min", "new-window", True, "1D")
    monkeypatch.setattr(ui, "selection_window", lambda *args: window)
    service = SimpleNamespace(refresh_seconds=5, history=lambda window: {"data": None})
    app = ui.create_app(service)
    render = callback(app, "pnl-chart")
    figure, _, _, cleared, note = render(
        {},
        ui.initial_ranges(),
        {"xaxis.range": ["old", "older"]},
        {"layout": {"uirevision": "old-window"}},
    )
    assert cleared is None
    assert note == ""
    assert figure.layout.xaxis.range is None
    assert figure.layout.uirevision == "new-window"


def test_funded_period_note_reaches_chart_without_replacing_valid_return(monkeypatch):
    window = Window(
        NOW - timedelta(days=2), NOW + timedelta(seconds=1), "1D", "funded", False, "ALL"
    )
    raw = {
        "timestamp": [
            (NOW - timedelta(days=2)).timestamp(),
            (NOW - timedelta(days=1)).timestamp(),
            NOW.timestamp(),
        ],
        "equity": [0, 1000, 1100],
        "base_value": 1000,
        "cashflow": {"CSD": [0, 1000, 0]},
    }
    monkeypatch.setattr(ui, "selection_window", lambda *args: window)
    app = ui.create_app(SimpleNamespace(refresh_seconds=5, history=lambda _: {"data": raw}))
    figure, summary, warning, _, note = callback(app, "pnl-chart")(
        {"as_of": NOW.isoformat(), "history_complete": True},
        ui.initial_ranges(),
        None,
        None,
    )
    assert summary["pnl"] == "100"
    assert warning == ""
    assert "first recorded nonzero balance" in note
    assert len(figure.data[0].x) == 2
