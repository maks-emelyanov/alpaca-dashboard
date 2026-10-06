"""Strategy analysis integration with range state, trade reconstruction and Dash."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("dash")
pytest.importorskip("dash_ag_grid")

from dash import no_update  # noqa: E402
from dash.development.base_component import Component  # noqa: E402

from alpaca_dashboard import ui  # noqa: E402
from alpaca_dashboard.models import Window  # noqa: E402


def callback(app, output_id):
    item = next(value for key, value in app.callback_map.items() if f"{output_id}." in key)
    return item["callback"].__wrapped__


def descendants(node):
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from descendants(child)
    elif isinstance(node, Component):
        yield node
        yield from descendants(getattr(node, "children", None))


def rendered_text(node):
    if isinstance(node, (list, tuple)):
        return " ".join(rendered_text(child) for child in node)
    if isinstance(node, Component):
        return rendered_text(getattr(node, "children", None))
    return "" if node is None else str(node)


def metric_text(content, metric):
    card = next(node for node in descendants(content) if getattr(node, "id", "") == metric)
    return rendered_text(card)


def execution(symbol, side, price, when):
    return {
        "id": f"{symbol}-{side}",
        "activity_type": "FILL",
        "symbol": symbol,
        "qty": "1",
        "price": str(price),
        "side": side,
        "transaction_time": when,
    }


@pytest.mark.parametrize("tab", ["trades", "positions", "orders"])
def test_analysis_does_not_fetch_history_while_another_tab_is_selected(tab):
    requests = []
    app = ui.create_app(
        SimpleNamespace(refresh_seconds=5, history=lambda window: requests.append(window))
    )
    result = callback(app, "analysis-content")({}, ui.initial_ranges(), tab)
    assert result is no_update
    assert requests == []


def test_analysis_uses_history_range_and_reconstructs_trades_before_filtering(monkeypatch):
    window = Window(
        datetime(2026, 9, 28, 4, tzinfo=UTC),
        datetime(2026, 10, 1, 4, tzinfo=UTC),
        "5Min",
        "analysis-test",
        False,
        "CUSTOM",
    )
    requested_selections, requests = [], []

    def selection(selection, snapshot):
        requested_selections.append(selection)
        return window

    def history(request):
        requests.append(request)
        return {"data": None, "loading": True}

    monkeypatch.setattr(ui, "selection_window", selection)
    app = ui.create_app(SimpleNamespace(refresh_seconds=5, history=history))
    ranges = {
        "chart": {"preset": "1D", "start_date": None, "end_date": None},
        "history": {
            "preset": "CUSTOM",
            "start_date": "2026-09-28",
            "end_date": "2026-09-30",
        },
        "linked": False,
    }
    snapshot = {
        "account": {"created_at": "2024-01-01T00:00:00Z"},
        "positions": [],
        "orders": [],
        "history_complete": True,
        "activities": [
            # The winning entry predates the range but its exit is in range.
            execution("AAPL", "buy", 100, "2026-09-25T15:00:00Z"),
            execution("AAPL", "sell", 120, "2026-09-28T15:00:00Z"),
            execution("MSFT", "buy", 100, "2026-09-29T14:00:00Z"),
            execution("MSFT", "sell", 90, "2026-09-29T15:00:00Z"),
            execution("NVDA", "buy", 100, "2026-09-24T14:00:00Z"),
            execution("NVDA", "sell", 50, "2026-09-25T15:00:00Z"),
            # An exit exactly at the exclusive end must not enter the sample.
            execution("META", "buy", 100, "2026-09-30T14:00:00Z"),
            execution("META", "sell", 200, "2026-10-01T04:00:00Z"),
        ],
    }

    content = callback(app, "analysis-content")(snapshot, ranges, "analysis")

    assert requested_selections == [ranges["history"]]
    assert len(requests) == 1
    assert requests[0].resolution == "1D"
    assert requests[0].start < window.start  # Fetch a prior balance for daily returns.
    assert requests[0].end == window.end
    assert "50.00%" in metric_text(content, "analysis-win_rate")
    assert "2.00" in metric_text(content, "analysis-profit_factor")
    assert "$5.00" in metric_text(content, "analysis-expectancy")
    assert "—" in metric_text(content, "analysis-sharpe")
    assert "2" in metric_text(content, "analysis-trade_count")
    assert all(
        getattr(node, "className", "") not in {"analysis-coverage", "analysis-methodology"}
        and getattr(node, "id", "") != "analysis-notes"
        for node in descendants(content)
    )
    text = rendered_text(content)
    assert "Daily returns:" not in text
    assert "incomplete records excluded" not in text
    assert "How these metrics are calculated" not in text
    assert "Account-wide daily equity includes" not in text
    assert "Available return sample:" in text


def test_analysis_missing_history_and_trades_explains_unavailable_values(monkeypatch):
    window = Window(
        datetime(2026, 9, 28, 4, tzinfo=UTC),
        datetime(2026, 9, 29, 4, tzinfo=UTC),
        "1Min",
        "analysis-empty",
        False,
        "1D",
    )
    monkeypatch.setattr(ui, "selection_window", lambda *args: window)
    service = SimpleNamespace(
        refresh_seconds=5,
        history=lambda _: {
            "data": None,
            "loading": True,
            "error": "Portfolio history temporarily unavailable",
        },
    )
    app = ui.create_app(service)
    content = callback(app, "analysis-content")(
        {"history_complete": True}, ui.initial_ranges(), "analysis"
    )

    for metric in ("win_rate", "sharpe", "cagr", "max_drawdown", "profit_factor", "expectancy"):
        assert "—" in metric_text(content, f"analysis-{metric}")
    status = next(
        node for node in descendants(content) if getattr(node, "id", "") == "analysis-status"
    )
    status_text = rendered_text(status).lower()
    assert "portfolio history temporarily unavailable" in status_text
    assert "loading" in status_text
    assert status.role == "status"
    assert status.className == "performance-warning"
    text = rendered_text(content).lower()
    assert "account" in text
    assert "completed" in text


def test_all_analysis_keeps_published_metrics_and_shows_actual_sample_end(monkeypatch):
    window = Window(
        datetime(2026, 9, 28, 19, 28, tzinfo=UTC),
        datetime(2026, 10, 5, 22, tzinfo=UTC),
        "15Min",
        "ALL",
        True,
        "ALL",
    )
    monkeypatch.setattr(ui, "selection_window", lambda *args: window)
    history = {
        "data": {
            # The Monday close is not published yet, despite the after-close fetch.
            "timestamp": [
                datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp()
                for day in ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03")
            ],
            "equity": [100, 110, 105, 115, 120],
            "base_value": 100,
            "base_value_asof": "2026-09-28",
            "cashflow": {},
        },
        "as_of": window.end.isoformat(),
        "loading": False,
    }
    app = ui.create_app(SimpleNamespace(refresh_seconds=5, history=lambda _: history))
    ranges = ui.initial_ranges()
    ranges["history"]["preset"] = "ALL"
    snapshot = {
        "account": {"created_at": window.start.isoformat(), "equity": "99999"},
        "as_of": window.end.isoformat(),
        "history_complete": True,
        "positions": [],
        "orders": [],
        "activities": [
            execution("AAPL", "buy", 100, "2026-10-05T14:00:00Z"),
            execution("AAPL", "sell", 105, "2026-10-05T15:00:00Z"),
        ],
    }

    content = callback(app, "analysis-content")(snapshot, ranges, "analysis")

    assert "20.00%" in metric_text(content, "analysis-total_return")
    assert "4.55%" in metric_text(content, "analysis-max_drawdown")
    for metric in ("sharpe", "sortino", "volatility"):
        assert "—" not in metric_text(content, f"analysis-{metric}")
    for metric in ("cagr", "calmar"):
        assert "365 days" in metric_text(content, f"analysis-{metric}")
    # Trade coverage still reaches the selected end, independently of daily data.
    assert "100.00%" in metric_text(content, "analysis-win_rate")
    assert "$5.00" in metric_text(content, "analysis-gross_pnl")
    text = rendered_text(content)
    assert "ALL · Sep 28, 2026 – Oct 05, 2026" in text
    assert "Available return sample: Sep 28, 2026 – Oct 02, 2026." in text
    assert "missing a required session close" not in text


def test_analysis_tab_hides_table_controls_and_restores_them_when_leaving():
    app = ui.create_app(SimpleNamespace(refresh_seconds=5))
    entry = next(item for key, item in app.callback_map.items() if "analysis-panel.style" in key)
    select_tab = entry["callback"].__wrapped__
    outputs = [output.component_id for output in entry["output"]]

    analysis = dict(zip(outputs, select_tab("analysis"), strict=True))
    assert analysis["analysis-panel"] == {}
    assert analysis["table-actions"] == {"display": "none"}
    assert all(
        analysis[f"{tab}-panel"] == {"display": "none"} for tab in ("trades", "positions", "orders")
    )
    assert "account-wide" in analysis["table-note"].lower()

    trades = dict(zip(outputs, select_tab("trades"), strict=True))
    assert trades["analysis-panel"] == {"display": "none"}
    assert trades["table-actions"] == {}
    assert trades["trades-panel"] == {}
