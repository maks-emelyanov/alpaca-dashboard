"""Presentation of strategy metrics and unavailable values."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from dash import html

from .models import number, timestamp
from .sessions import NEW_YORK

PRIMARY_METRICS = ("win_rate", "sharpe", "cagr", "max_drawdown")
METRIC_CAPTIONS = {
    "win_rate": "Winning trades / total",
    "sharpe": "Return / volatility",
    "cagr": "Annualized growth",
    "max_drawdown": "Peak-to-trough decline",
    "total_return": "Compounded return (est.)",
    "sortino": "Return / downside risk",
    "volatility": "Annualized variation",
    "calmar": "CAGR / max drawdown",
    "trade_count": "Trades closed in range",
    "profit_factor": "Gross profit / gross loss",
    "expectancy": "Average P&L per trade",
    "payoff_ratio": "Avg win / avg loss",
    "average_win": "Profit per winning trade",
    "average_loss": "Loss per losing trade",
    "gross_pnl": "Total realized P&L",
    "average_duration": "Average time in a trade",
}


def _value(metric):
    value = number(metric.get("value"))
    if value is None:
        return "—"
    unit = metric["unit"]
    if unit == "money":
        return f"{'−' if value < 0 else ''}${abs(value):,.2f}"
    if unit == "percent":
        return f"{value:,.2f}%"
    if unit == "count":
        return f"{value:,.0f}"
    if unit == "seconds":
        if value >= 86400:
            return f"{value / Decimal(86400):,.1f} days"
        if value >= 3600:
            return f"{value / Decimal(3600):,.1f} hours"
        return f"{value / Decimal(60):,.1f} min"
    return f"{value:,.2f}"


def _card(key, metric):
    reason = metric.get("reason")
    return html.Div(
        [
            html.Div(metric["label"], className="analysis-metric-label"),
            html.Div(_value(metric), className="analysis-metric-value"),
            html.P(
                METRIC_CAPTIONS[key],
                title=metric["definition"],
                className="analysis-metric-definition",
            ),
            html.P(reason, className="analysis-metric-reason") if reason else None,
        ],
        id=f"analysis-{key}",
        className="analysis-metric" + (" is-unavailable" if metric.get("value") is None else ""),
    )


def _date_label(value):
    if not value:
        return "—"
    return timestamp(value).astimezone(NEW_YORK).strftime("%b %d, %Y")


def analysis_content(result, window, history):
    metrics = result["metrics"]
    coverage = result["coverage"]
    status = []
    if history.get("loading"):
        status.append("Loading daily account history. Available trade statistics appear below.")
    if history.get("error"):
        status.append(f"Daily history refresh paused. {history['error']}")
    period = (
        f"{window.preset} · {_date_label(window.start)} – "
        f"{_date_label(window.end - timedelta(microseconds=1))}"
    )
    return [
        html.Div(
            [
                html.Div(
                    [
                        html.H3("Performance overview", className="analysis-section-title"),
                        html.P(
                            "Account-wide returns, risk, and trade outcomes "
                            "for the selected period.",
                            className="analysis-section-note",
                        ),
                    ]
                ),
                html.Span(period, id="analysis-period", className="analysis-period"),
            ],
            className="analysis-heading",
        ),
        html.Div(
            " · ".join(status),
            id="analysis-status",
            className="performance-warning",
            role="status",
        ),
        html.Div(
            [_card(key, metrics[key]) for key in PRIMARY_METRICS],
            className="analysis-grid analysis-primary",
        ),
        html.H3("Account returns & risk", className="analysis-section-title"),
        html.P(
            "Daily equity includes open positions, fees and dividends. "
            f"Available return sample: {_date_label(coverage.get('start'))} – "
            f"{_date_label(coverage.get('end'))}.",
            className="analysis-section-note",
        ),
        html.Div(
            [
                _card(key, metric)
                for key, metric in metrics.items()
                if metric["group"] == "equity" and key not in PRIMARY_METRICS
            ],
            className="analysis-grid",
        ),
        html.H3("Trade outcomes", className="analysis-section-title"),
        html.P(
            "Completed position lifecycles, selected by exit time. Gross execution P&L "
            "excludes fees and open positions.",
            className="analysis-section-note",
        ),
        html.Div(
            [
                _card(key, metric)
                for key, metric in metrics.items()
                if metric["group"] == "trades" and key not in PRIMARY_METRICS
            ],
            className="analysis-grid",
        ),
    ]
