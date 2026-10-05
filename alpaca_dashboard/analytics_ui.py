"""Presentation of strategy metrics, including sample sizes and unavailable values."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from dash import html

from .models import number, timestamp
from .sessions import NEW_YORK

PRIMARY_METRICS = ("win_rate", "sharpe", "cagr", "max_drawdown")


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
            html.P(metric["definition"], className="analysis-metric-definition"),
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
    notes = list(result.get("notes", []))
    if history.get("loading"):
        notes.insert(0, "Loading daily account history. Available trade statistics appear below.")
    if history.get("error"):
        notes.insert(0, f"Daily history refresh paused. {history['error']}")
    period = (
        f"{window.preset} · {_date_label(window.start)} – "
        f"{_date_label(window.end - timedelta(microseconds=1))}"
    )
    return [
        html.Div(
            [
                html.Div(
                    [
                        html.Div("PERFORMANCE & RISK", className="eyebrow"),
                        html.H2("Strategy analysis"),
                        html.P(
                            "Assess the account as a whole. If multiple strategies share this "
                            "account, these metrics combine their results."
                        ),
                    ]
                ),
                html.Span(period, id="analysis-period", className="analysis-period"),
            ],
            className="analysis-heading",
        ),
        html.Div(
            [
                html.Span(f"Daily returns: {coverage['used_days']}"),
                html.Span(f"Completed trades: {coverage['valid_trade_count']}"),
                html.Span(
                    f"Wins: {coverage['wins']} / Losses: {coverage['losses']} / "
                    f"Breakeven: {coverage['breakeven']}"
                ),
                html.Span(f"{coverage['excluded']} incomplete records excluded"),
            ],
            className="analysis-coverage",
        ),
        html.Div(
            html.Ul([html.Li(note) for note in dict.fromkeys(notes)]) if notes else None,
            id="analysis-notes",
            className="analysis-notes",
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
            "excludes fees and open positions; grid search and filters do not change "
            "these metrics.",
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
        html.Details(
            [
                html.Summary("How these metrics are calculated"),
                html.P(
                    "Daily returns remove external deposits and withdrawals using an "
                    "end-of-period cash-flow convention, then compound the adjusted returns. "
                    "Funding during a session can make this an approximation. "
                    "Only completed NYSE sessions are used; intraday drawdowns are not captured."
                ),
                html.P(
                    "Sharpe and Sortino assume a 0% risk-free rate and target return, with "
                    "252 trading days per year. CAGR uses elapsed calendar time and requires "
                    "at least one year of history. Unavailable or undefined results appear as —."
                ),
                html.P(
                    "Use sample size, losses and drawdowns together with returns. Paper account "
                    "results do not measure live execution quality or prove future profitability."
                ),
            ],
            className="analysis-methodology",
        ),
    ]
