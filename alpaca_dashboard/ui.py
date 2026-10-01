"""Local, read-only account dashboard built with Dash and AG Grid Community."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import dash_ag_grid as dag
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from alpaca_dashboard.accounting import order_rows, position_rows, trade_rows
from alpaca_dashboard.performance import performance_series
from alpaca_dashboard.timeframes import resolve_window

NY = ZoneInfo("America/New_York")
PRESETS = ("1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "ALL", "CUSTOM")
GREEN = "#35d498"
RED = "#ff777d"
NUMERIC_FIELDS = {
    "quantity",
    "entry_price",
    "exit_price",
    "duration_seconds",
    "realized_pnl",
    "realized_pct",
    "qty",
    "qty_available",
    "avg_entry_price",
    "current_price",
    "cost_basis",
    "market_value",
    "unrealized_pl",
    "unrealized_pct",
    "unrealized_intraday_pl",
    "stop_price",
    "target_price",
    "filled_qty",
    "filled_avg_price",
    "limit_price",
}


def parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def account_inception(snapshot, now):
    created = parse_time((snapshot.get("account") or {}).get("created_at"))
    if created:
        return created
    dates = [
        parse_time(row.get("transaction_time") or row.get("date"))
        for row in snapshot.get("activities", [])
    ]
    return min((stamp for stamp in dates if stamp), default=now)


def initial_ranges():
    selected = {"preset": "1D", "start_date": None, "end_date": None}
    return {"chart": dict(selected), "history": dict(selected), "linked": True}


def selection_window(selection, snapshot, now=None):
    now = now or datetime.now(UTC)
    return resolve_window(
        selection["preset"],
        now,
        account_inception(snapshot, now),
        start_date=selection.get("start_date"),
        end_date=selection.get("end_date"),
    )


def change_ranges(trigger, chart, history, linked, accepted, snapshot, now=None):
    """Validate draft controls before committing a new visible range.

    The controls keep incomplete custom input while the accepted ranges keep the
    previous chart and rows. Linking applies the chart's accepted selection.
    """
    result = deepcopy(accepted or initial_ranges())
    drafts = {"chart": dict(chart), "history": dict(history)}
    errors = {"chart": "", "history": ""}
    if trigger == "link-ranges":
        result["linked"] = linked
        if linked:
            result["history"] = dict(result["chart"])
            drafts = {key: dict(result["chart"]) for key in drafts}
        return drafts, result, errors
    source = "history" if str(trigger).startswith("history-") else "chart"
    selected = drafts[source]
    result["linked"] = linked
    try:
        selection_window(selected, snapshot, now)
    except (ValueError, TypeError) as exc:
        errors[source] = str(exc)
        return drafts, result, errors
    result[source] = dict(selected)
    if linked:
        target = "history" if source == "chart" else "chart"
        result[target] = dict(selected)
        drafts[target] = dict(selected)
    return drafts, result, errors


def filter_rows(rows, field, window):
    """Rows are filtered only after complete trade reconstruction."""
    result = []
    for row in rows:
        stamp = parse_time(row.get(field))
        if stamp and window.start <= stamp < window.end:
            result.append(row)
        elif not stamp and str(row.get("status", "")).lower().startswith("incomplete"):
            result.append(row)
    return result


def grid_rows(rows):
    """Decimal strings become numbers only at the UI presentation boundary."""
    result = []
    for source in rows:
        row = dict(source)
        for field in NUMERIC_FIELDS & row.keys():
            try:
                number = Decimal(str(row[field]))
                row[field] = float(number) if number.is_finite() else None
            except (InvalidOperation, ValueError, TypeError):
                if field not in {"stop_price", "target_price"} or "," not in str(row[field]):
                    row[field] = None
        result.append(row)
    return result


def money(value, signed=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "—"
    except (InvalidOperation, ValueError, TypeError):
        return "—"
    sign = "−" if number < 0 else "+" if signed else ""
    return f"{sign}${abs(number):,.2f}"


def timestamp_label(value):
    stamp = parse_time(value)
    return stamp.astimezone(NY).strftime("%b %d, %Y · %I:%M:%S %p ET") if stamp else "—"


def chart_figure(series, window):
    points = [point for point in series.get("points", []) if point.get("pnl") is not None]
    pnl = Decimal(str(series.get("pnl") or "0"))
    color = GREEN if pnl >= 0 else RED
    figure = go.Figure()
    if points:
        x = [parse_time(point["timestamp"]).astimezone(NY).replace(tzinfo=None) for point in points]
        y = [float(point["pnl"]) for point in points]
        figure.add_trace(
            go.Scatter(
                x=x,
                y=y,
                uid="account-performance",
                mode="lines",
                line={"color": color, "width": 2.5},
                fill="tozeroy",
                fillcolor="rgba(53,212,152,.055)" if pnl >= 0 else "rgba(255,119,125,.055)",
                customdata=[
                    [
                        point["pnl"],
                        point.get("pnl_pct"),
                        timestamp_label(point["timestamp"]),
                        money(point.get("equity")),
                    ]
                    for point in points
                ],
                hovertemplate=(
                    "%{customdata[2]}<br>P&L %{y:$,.2f}<br>Equity %{customdata[3]}<extra></extra>"
                ),
            )
        )
        figure.add_hline(y=0, line_color="#303733", line_dash="dot", line_width=1)
    else:
        figure.add_annotation(
            text="Waiting for account history"
            if not series.get("error")
            else "Performance is unavailable for this period",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"color": "#8e9e94", "size": 15},
        )
    figure.update_layout(
        template=None,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, ui-sans-serif, system-ui, sans-serif", "color": "#8e9e94"},
        margin={"l": 8, "r": 8, "t": 20, "b": 35},
        height=345,
        showlegend=False,
        hovermode="x",
        hoverdistance=-1,  # Track the nearest timestamp across gaps in sparse history.
        dragmode="zoom",
        uirevision=window.key,
        hoverlabel={"bgcolor": "#202a24", "bordercolor": "#39473f", "font_size": 12},
        xaxis={
            "showgrid": False,
            "zeroline": False,
            "showline": False,
            "fixedrange": False,
            "autorange": True,
            "showspikes": True,
            "spikemode": "across",
            "spikesnap": "cursor",
            "spikecolor": "#697b70",
            "spikethickness": 1,
            "spikedash": "solid",
            "tickfont": {"size": 11},
            "nticks": 6,
        },
        yaxis={
            "showgrid": False,
            "zeroline": False,
            "showticklabels": False,
            "fixedrange": False,
            "autorange": True,
        },
    )
    return figure


def preserve_viewport(figure, relayout):
    """Keep explicit user zoom across Plotly reactive figure replacements.

    ``uirevision`` remains the primary identity; Dash's relayout payload also
    restores ranges when the installed Plotly version resets axis UI state.
    The caller clears this payload whenever the selected timeframe changes.
    """
    for axis in ("xaxis", "yaxis"):
        if relayout.get(f"{axis}.autorange") is True:
            figure.layout[axis].update(autorange=True)
            continue
        bounds = relayout.get(f"{axis}.range")
        if bounds is None and f"{axis}.range[0]" in relayout:
            bounds = [relayout.get(f"{axis}.range[0]"), relayout.get(f"{axis}.range[1]")]
        if bounds is not None and len(bounds) == 2 and all(value is not None for value in bounds):
            figure.layout[axis].update(range=bounds, autorange=False)


def _column(field, title, kind="text", width=150):
    if kind in {"money", "percent", "number"} and width == 150:
        width = 130
    column = {"field": field, "headerName": title, "minWidth": width, "width": width}
    if kind in {"money", "percent", "number", "duration"}:
        column.update(type="numericColumn", filter="agNumberColumnFilter")
        column["valueFormatter"] = {"function": f"dashboard{kind.title()}(params.value)"}
        if field in {
            "realized_pnl",
            "realized_pct",
            "unrealized_pl",
            "unrealized_pct",
            "unrealized_intraday_pl",
        }:
            column["cellClassRules"] = {
                "positive-value": "params.value > 0",
                "negative-value": "params.value < 0",
            }
    elif kind == "prices":
        column["valueFormatter"] = {"function": "dashboardPrices(params.value)"}
    elif kind == "date":
        column["valueFormatter"] = {"function": "dashboardDate(params.value)"}
        column["minWidth"] = column["width"] = 200
    if field == "symbol":
        column.update(pinned="left", minWidth=110, width=110, cellClass="symbol-cell")
    return column


TRADE_COLUMNS = [
    _column("symbol", "Symbol"),
    _column("direction", "Direction", width=110),
    _column("quantity", "Quantity", "number"),
    _column("realized_pnl", "Gross P&L", "money"),
    _column("realized_pct", "Return", "percent", 120),
    _column("entry_price", "Avg entry", "money"),
    _column("exit_price", "Avg exit", "money"),
    _column("opened_at", "Entry · ET", "date"),
    _column("closed_at", "Exit · ET", "date"),
    _column("duration_seconds", "Held", "duration", 120),
    _column("status", "Data status"),
]
POSITION_COLUMNS = [
    _column("symbol", "Symbol"),
    _column("side", "Side", width=100),
    _column("qty", "Quantity", "number"),
    _column("market_value", "Market value", "money"),
    _column("unrealized_pl", "Unrealized P&L", "money", 170),
    _column("unrealized_pct", "Return", "percent"),
    _column("avg_entry_price", "Avg entry", "money"),
    _column("current_price", "Last price", "money"),
    _column("qty_available", "Available", "number"),
    _column("cost_basis", "Cost basis", "money"),
    _column("unrealized_intraday_pl", "Today’s P&L", "money"),
    _column("stop_price", "Linked stops", "prices"),
    _column("target_price", "Linked targets", "prices"),
]
ORDER_COLUMNS = [
    _column("symbol", "Symbol"),
    _column("submitted_at", "Submitted · ET", "date"),
    _column("side", "Side", width=100),
    _column("type", "Type", width=120),
    _column("qty", "Requested", "number"),
    _column("filled_qty", "Filled", "number"),
    _column("filled_avg_price", "Avg fill", "money"),
    _column("limit_price", "Limit", "money"),
    _column("stop_price", "Stop", "money"),
    _column("status", "Status"),
    _column("parent_id", "Parent / bracket", width=220),
    _column("id", "Order ID", width=260),
]


def _grid(name, columns):
    return dag.AgGrid(
        id=f"{name}-grid",
        columnDefs=columns,
        rowData=[],
        getRowId="params.data.id",
        defaultColDef={"sortable": True, "filter": True, "resizable": True},
        dashGridOptions={
            "domLayout": "autoHeight",
            "pagination": True,
            "paginationPageSize": 25,
            "paginationPageSizeSelector": [25, 50, 100],
            "animateRows": False,
            "rowSelection": {
                "mode": "singleRow",
                "enableClickSelection": True,
                "checkboxes": False,
            },
            "localeText": {"noRowsToShow": "No records in this view"},
            "suppressScrollOnNewData": True,
            # Initialize headers and viewport even when the grid starts below the fold.
            "suppressContentVisibilityAuto": True,
            "theme": {"function": "dashboardTheme(themeQuartz)"},
        },
        className="account-grid",
        style={"height": "auto"},
        persistence=True,
        persistence_type="session",
        persisted_props=["filterModel", "selectedRows"],
        csvExportParams={"fileName": f"alpaca-{name}.csv"},
    )


def _range_controls(name):
    return html.Div(
        [
            dcc.RadioItems(
                id=f"{name}-preset",
                options=list(PRESETS),
                value="1D",
                inline=True,
                className="timeframes",
                inputClassName="timeframe-input",
                labelClassName="timeframe-label",
            ),
            html.Div(
                dcc.DatePickerRange(
                    id=f"{name}-dates",
                    display_format="MMM D, YYYY",
                    minimum_nights=0,
                    start_date_placeholder_text="Start date",
                    end_date_placeholder_text="End date",
                    clearable=True,
                    number_of_months_shown=1,
                ),
                id=f"{name}-custom",
                className="custom-dates",
                style={"display": "none"},
            ),
            html.Div(id=f"{name}-range-error", className="field-error", role="alert"),
        ],
        className="range-controls",
    )


def _layout(refresh_seconds):
    return html.Main(
        [
            dcc.Interval(id="refresh", interval=max(1, int(refresh_seconds * 1000)), n_intervals=0),
            dcc.Store(id="snapshot", data={}),
            dcc.Store(id="accepted-ranges", data=initial_ranges()),
            dcc.Store(id="performance", data={}),
            html.Header(
                [
                    html.Div(
                        [
                            html.Span("A", className="brand-mark"),
                            html.Div(
                                [
                                    html.Div("Alpaca Dashboard", className="brand-name"),
                                    html.Div("Account activity", className="brand-subtitle"),
                                ]
                            ),
                        ],
                        className="brand",
                    ),
                    html.Div(
                        [
                            html.Span("PAPER ACCOUNT", className="paper-badge"),
                            html.Span("Connecting", id="market-status", className="market-status"),
                        ],
                        className="header-meta",
                    ),
                ],
                className="page-header",
            ),
            html.Div(id="status-banner", className="status-banner", role="status"),
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div("ACCOUNT VALUE", className="eyebrow"),
                                    html.H1("—", id="account-equity", className="account-value"),
                                    html.Div(
                                        [
                                            html.Span(
                                                [
                                                    html.Span("—", id="pnl-value"),
                                                    html.Span("(—)", id="pnl-percent"),
                                                ],
                                                className="pnl-change",
                                            ),
                                            html.Span(
                                                "Today", id="pnl-period", className="period-label"
                                            ),
                                        ],
                                        className="pnl-caption",
                                    ),
                                ],
                                className="performance-headline",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Span("Buying power", className="metric-label"),
                                            html.Strong(
                                                "—", id="buying-power", className="metric-value"
                                            ),
                                        ]
                                    ),
                                ],
                                className="account-metrics",
                            ),
                        ],
                        className="performance-summary",
                    ),
                    dcc.Graph(
                        id="pnl-chart",
                        config={"displayModeBar": False, "scrollZoom": False, "responsive": True},
                        responsive=True,
                        clear_on_unhover=True,
                        className="performance-chart",
                        style={"height": "345px", "width": "100%"},
                    ),
                    _range_controls("chart"),
                    html.Div(id="performance-note", className="performance-note"),
                    html.Div(
                        id="performance-warning", className="performance-warning", role="status"
                    ),
                    html.Div(
                        [
                            html.Span("Cash-flow adjusted · USD", className="chart-note"),
                            html.Span("Waiting for first refresh", id="last-refresh"),
                        ],
                        className="chart-footer",
                    ),
                ],
                className="performance-card",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H2("Your activity"),
                                    html.P("Every execution. Every position. One account."),
                                ]
                            ),
                            dcc.Checklist(
                                id="link-ranges",
                                options=[{"label": "Link timeframes", "value": "linked"}],
                                value=["linked"],
                                className="link-ranges",
                                inline=True,
                            ),
                        ],
                        className="activity-heading",
                    ),
                    dcc.Tabs(
                        id="activity-tab",
                        value="trades",
                        className="activity-tabs",
                        children=[
                            dcc.Tab(
                                label="Trade history",
                                value="trades",
                                className="activity-tab",
                                selected_className="activity-tab-selected",
                            ),
                            dcc.Tab(
                                label="Open now",
                                value="positions",
                                className="activity-tab",
                                selected_className="activity-tab-selected",
                            ),
                            dcc.Tab(
                                label="Orders",
                                value="orders",
                                className="activity-tab",
                                selected_className="activity-tab-selected",
                            ),
                        ],
                    ),
                    html.Div(
                        [
                            html.Div(
                                _range_controls("history"),
                                id="history-range-controls",
                                style={"display": "none"},
                            ),
                            html.Div(
                                [
                                    dcc.Input(
                                        id="table-search",
                                        placeholder="Search activity…",
                                        type="search",
                                        debounce=False,
                                        className="table-search",
                                    ),
                                    html.Button(
                                        "Export CSV ↗",
                                        id="export-csv",
                                        n_clicks=0,
                                        className="export-button",
                                    ),
                                ],
                                className="table-actions",
                            ),
                        ],
                        className="table-toolbar",
                    ),
                    html.Div(id="table-note", className="table-note"),
                    html.Div(_grid("trades", TRADE_COLUMNS), id="trades-panel"),
                    html.Div(
                        _grid("positions", POSITION_COLUMNS),
                        id="positions-panel",
                        style={"display": "none"},
                    ),
                    html.Div(
                        _grid("orders", ORDER_COLUMNS), id="orders-panel", style={"display": "none"}
                    ),
                    html.Div(
                        id="trade-details", className="trade-details", style={"display": "none"}
                    ),
                ],
                className="activity-card",
            ),
            html.Footer(
                [
                    html.Span("Alpaca paper · Read-only account view"),
                    html.Span("Trade returns are gross execution P&L · Times in New York"),
                ],
                className="page-footer",
            ),
        ],
        className="dashboard-shell",
    )


def create_app(service):
    """Create a dashboard; the injected service owns all broker and cache access."""
    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).parent / "assets"),
        title="Alpaca Dashboard · Account activity",
        update_title=None,
    )
    app.layout = lambda: _layout(service.refresh_seconds)

    @app.callback(Output("snapshot", "data"), Input("refresh", "n_intervals"))
    def refresh(_):
        return service.snapshot()

    @app.callback(
        Output("chart-preset", "value"),
        Output("chart-dates", "start_date"),
        Output("chart-dates", "end_date"),
        Output("history-preset", "value"),
        Output("history-dates", "start_date"),
        Output("history-dates", "end_date"),
        Output("accepted-ranges", "data"),
        Output("chart-range-error", "children"),
        Output("history-range-error", "children"),
        Output("chart-custom", "style"),
        Output("history-custom", "style"),
        Output("history-range-controls", "style"),
        Input("chart-preset", "value"),
        Input("chart-dates", "start_date"),
        Input("chart-dates", "end_date"),
        Input("history-preset", "value"),
        Input("history-dates", "start_date"),
        Input("history-dates", "end_date"),
        Input("link-ranges", "value"),
        State("accepted-ranges", "data"),
        State("snapshot", "data"),
    )
    def sync_ranges(cp, cs, ce, hp, hs, he, linked, accepted, snapshot):
        drafts, result, errors = change_ranges(
            ctx.triggered_id,
            {"preset": cp, "start_date": cs, "end_date": ce},
            {"preset": hp, "start_date": hs, "end_date": he},
            "linked" in linked,
            accepted,
            snapshot or {},
        )
        c, h = drafts["chart"], drafts["history"]
        return (
            c["preset"],
            c["start_date"],
            c["end_date"],
            h["preset"],
            h["start_date"],
            h["end_date"],
            result,
            errors["chart"],
            errors["history"],
            {} if c["preset"] == "CUSTOM" else {"display": "none"},
            {} if h["preset"] == "CUSTOM" else {"display": "none"},
            {"display": "none"} if result["linked"] else {},
        )

    @app.callback(
        Output("pnl-chart", "figure"),
        Output("performance", "data"),
        Output("performance-warning", "children"),
        Output("pnl-chart", "relayoutData"),
        Output("performance-note", "children"),
        Input("snapshot", "data"),
        Input("accepted-ranges", "data"),
        State("pnl-chart", "relayoutData"),
        State("pnl-chart", "figure"),
    )
    def render_chart(snapshot, ranges, relayout, previous):
        snapshot = snapshot or {}
        selected = (ranges or initial_ranges())["chart"]
        window = selection_window(selected, snapshot)
        history = service.history(window)
        series = performance_series(
            history.get("data"),
            window,
            account=snapshot.get("account"),
            activities=snapshot.get("activities", []),
            as_of=snapshot.get("as_of"),
            history_complete=snapshot.get("history_complete", False),
        )
        labels = {
            "1D": "Today / latest session",
            "1W": "Past week",
            "1M": "Past month",
            "3M": "Past 3 months",
            "6M": "Past 6 months",
            "YTD": "Year to date",
            "1Y": "Past year",
            "ALL": "All time",
            "CUSTOM": "Custom period",
        }
        headline = {
            "pnl": series.get("pnl"),
            "pnl_pct": series.get("pnl_pct"),
            "label": labels[selected["preset"]],
        }
        errors = [str(error) for error in (history.get("error"), series.get("error")) if error]
        figure = chart_figure(series, window)
        same_window = (previous or {}).get("layout", {}).get("uirevision") == window.key
        if same_window:
            preserve_viewport(figure, relayout or {})
        return (
            figure,
            headline,
            " · ".join(dict.fromkeys(errors)),
            no_update if same_window else None,
            series.get("note") or "",
        )

    @app.callback(
        Output("buying-power", "children"),
        Output("last-refresh", "children"),
        Output("status-banner", "children"),
        Output("status-banner", "style"),
        Output("market-status", "children"),
        Input("snapshot", "data"),
    )
    def render_status(snapshot):
        snapshot = snapshot or {}
        account = snapshot.get("account") or {}
        messages = []
        if snapshot.get("error"):
            messages.append(f"Refresh paused · Showing last available data. {snapshot['error']}")
        if snapshot.get("loading"):
            messages.append("Loading account history. Your current positions will appear first.")
        elif not snapshot.get("history_complete", True):
            messages.append("History is incomplete. Some trade returns may be unavailable.")
        if not account and not messages:
            messages.append("Connecting to your paper account…")
        clock = snapshot.get("clock") or {}
        market = "Market open" if clock.get("is_open") else "Market closed"
        if not clock:
            market = "New York · Regular session"
        return (
            money(account.get("buying_power")),
            f"Updated {timestamp_label(snapshot.get('as_of'))}",
            " ".join(messages),
            {} if messages else {"display": "none"},
            market,
        )

    @app.callback(
        Output("trades-grid", "rowData"),
        Output("positions-grid", "rowData"),
        Output("orders-grid", "rowData"),
        Input("snapshot", "data"),
        Input("accepted-ranges", "data"),
    )
    def render_tables(snapshot, ranges):
        snapshot = snapshot or {}
        positions, orders = snapshot.get("positions", []), snapshot.get("orders", [])
        activities = snapshot.get("activities", [])
        window = selection_window((ranges or initial_ranges())["history"], snapshot)
        trades = trade_rows(
            activities,
            positions,
            orders,
            history_complete=snapshot.get("history_complete", False),
        )
        holdings = position_rows(positions, orders)
        order_data = order_rows(orders)
        return (
            grid_rows(filter_rows(trades, "closed_at", window)),
            grid_rows(holdings),
            grid_rows(filter_rows(order_data, "submitted_at", window)),
        )

    @app.callback(
        Output("trades-panel", "style"),
        Output("positions-panel", "style"),
        Output("orders-panel", "style"),
        Output("table-note", "children"),
        Input("activity-tab", "value"),
    )
    def select_tab(tab):
        notes = {
            "trades": ("Completed trades by exit time · Select a row for execution details"),
            "positions": (
                "All current holdings · Always visible, regardless of the history timeframe"
            ),
            "orders": "Orders by submission time · Includes bracket and child orders",
        }
        return (
            *(
                {} if tab == name else {"display": "none"}
                for name in ("trades", "positions", "orders")
            ),
            notes[tab],
        )

    @app.callback(
        Output("trades-grid", "exportDataAsCsv"),
        Output("positions-grid", "exportDataAsCsv"),
        Output("orders-grid", "exportDataAsCsv"),
        Input("export-csv", "n_clicks"),
        State("activity-tab", "value"),
        prevent_initial_call=True,
    )
    def export(_, tab):
        return tuple(
            True if tab == name else no_update for name in ("trades", "positions", "orders")
        )

    @app.callback(
        Output("trade-details", "children"),
        Output("trade-details", "style"),
        Input("trades-grid", "selectedRows"),
        Input("activity-tab", "value"),
    )
    def details(selected, tab):
        if tab != "trades" or not selected:
            return [], {"display": "none"}
        row = selected[0]
        fills = row.get("details") or []
        fill_table = html.Div(
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th(label)
                                for label in (
                                    "Executed · ET",
                                    "Side",
                                    "Allocation",
                                    "Quantity",
                                    "Price",
                                    "Order",
                                )
                            ]
                        )
                    ),
                    html.Tbody(
                        [
                            html.Tr(
                                [
                                    html.Td(timestamp_label(fill.get("transaction_time"))),
                                    html.Td(fill.get("side", "—")),
                                    html.Td(fill.get("allocation", "—")),
                                    html.Td(fill.get("qty", "—")),
                                    html.Td(money(fill.get("price"))),
                                    html.Td(fill.get("order_id", "—")),
                                ]
                            )
                            for fill in fills
                        ]
                    ),
                ],
                className="fills-table",
            ),
            className="fills-scroll",
        )
        return [
            html.Div(
                [
                    html.H3(f"{row.get('symbol', '')} · Execution details"),
                    html.Span("Gross execution P&L", className="chart-note"),
                ],
                className="details-heading",
            ),
            html.P(
                row.get("status", ""),
                className="chart-note",
            ),
            fill_table,
            html.Details(
                [
                    html.Summary("Complete execution records"),
                    html.Pre(
                        json.dumps(
                            {
                                "fills": fills,
                                "status": row.get("status"),
                            },
                            indent=2,
                            default=str,
                        )
                    ),
                ]
            ),
        ], {}

    app.clientside_callback(
        """function(hover, summary, snapshot) {
            summary = summary || {};
            const currency = new Intl.NumberFormat('en-US', {
                style: 'currency', currency: 'USD'
            });
            const equity = snapshot && snapshot.account ? snapshot.account.equity : null;
            let value = equity !== null && equity !== undefined && equity !== ''
                && Number.isFinite(Number(equity)) ? currency.format(Number(equity)) : '—';
            let pnl = summary.pnl, pct = summary.pnl_pct, label = summary.label || 'Today';
            if (hover && hover.points && hover.points[0].customdata) {
                const data = hover.points[0].customdata;
                pnl = data[0]; pct = data[1]; label = data[2];
                value = data[3] || '—';
            }
            const valid = pnl !== null && pnl !== undefined && Number.isFinite(Number(pnl));
            const amount = valid ? Number(pnl) : null;
            const sign = amount < 0 ? '−' : '+';
            const text = valid ? sign + currency.format(Math.abs(amount)) : '—';
            const percent = pct !== null && pct !== undefined && Number.isFinite(Number(pct))
                ? (Number(pct) < 0 ? '−' : '+') + Math.abs(Number(pct)).toFixed(2) + '%' : '—';
            const color = valid ? (amount < 0 ? ' negative-value' : ' positive-value') : '';
            return [value, text, '(' + percent + ')', label, 'pnl-value' + color, color.trim()];
        }""",
        Output("account-equity", "children"),
        Output("pnl-value", "children"),
        Output("pnl-percent", "children"),
        Output("pnl-period", "children"),
        Output("pnl-value", "className"),
        Output("pnl-percent", "className"),
        Input("pnl-chart", "hoverData"),
        Input("performance", "data"),
        Input("snapshot", "data"),
    )
    app.clientside_callback(
        """function(search, trades, positions, orders) {
            return [trades, positions, orders].map(options =>
                Object.assign({}, options, {quickFilterText: search || ''}));
        }""",
        Output("trades-grid", "dashGridOptions"),
        Output("positions-grid", "dashGridOptions"),
        Output("orders-grid", "dashGridOptions"),
        Input("table-search", "value"),
        State("trades-grid", "dashGridOptions"),
        State("positions-grid", "dashGridOptions"),
        State("orders-grid", "dashGridOptions"),
        prevent_initial_call=True,
    )
    return app
