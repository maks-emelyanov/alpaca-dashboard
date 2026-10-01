"""Real-browser checks against synthetic data; no brokerage credentials or requests."""

from threading import Thread

import pytest

pytest.importorskip("dash")
pytest.importorskip("dash_ag_grid")
playwright = pytest.importorskip("playwright.sync_api")

from dashboard_fakes import DemoDashboardService  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from alpaca_dashboard.ui import create_app  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Install browser with: uv run playwright install chromium")
            raise
        yield instance
        instance.close()


@pytest.fixture
def dashboard(browser, request):
    options = dict(getattr(request, "param", {}))
    viewport = options.pop("viewport", {"width": 1440, "height": 1100})
    service = DemoDashboardService(**options)
    app = create_app(service)
    server = make_server("127.0.0.1", 0, app.server, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"http://127.0.0.1:{server.server_port}")
    playwright.expect(page.locator("#account-equity")).to_have_text("$101,250.00")
    playwright.expect(page).to_have_title("Alpaca Dashboard · Account activity")
    page.wait_for_function("window.dash_ag_grid && window.dash_ag_grid.getApi('trades-grid')")
    yield page, service, errors
    page.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def select_preset(page, component, value):
    page.locator(f"#{component}-preset").get_by_text(value, exact=True).click()


def checked_preset(page, component):
    return page.locator(f"#{component}-preset input:checked").evaluate("input => input.value")


@pytest.mark.parametrize(
    "dashboard",
    [
        {
            "previous_session": True,
            "refresh_seconds": 3600,
            "viewport": {"width": 1280, "height": 600},
        }
    ],
    indirect=True,
)
def test_timeframe_populates_empty_visible_trade_table_without_switching_tabs(dashboard):
    page, service, errors = dashboard
    snapshot_calls = service.calls
    empty = page.locator("#trades-grid .ag-overlay-no-rows-center")
    symbol = page.locator('#trades-grid .ag-pinned-left-cols-container [col-id="symbol"]')
    direction = page.locator('#trades-grid .ag-center-cols-container [col-id="direction"]')
    headers = page.locator("#trades-grid .ag-header-cell-text")

    # The first grid starts below the fold. Do not resize or switch tabs to
    # trigger a layout repair before checking its initial headers and rows.
    page.wait_for_function("""() => ['trades', 'positions', 'orders'].every(name => {
        try { return Boolean(window.dash_ag_grid.getApi(name + '-grid')); }
        catch (_) { return false; }
    })""")
    rendering = page.evaluate("""() => ['trades', 'positions', 'orders'].map(name =>
        getComputedStyle(document.querySelector('#' + name + '-grid .ag-root-wrapper'))
            .contentVisibility)
    """)
    assert rendering == ["visible", "visible", "visible"]
    playwright.expect(headers.filter(has_text="Symbol")).to_be_attached()
    playwright.expect(headers.filter(has_text="Direction")).to_be_attached()
    page.mouse.move(20, 300)
    page.mouse.wheel(0, 700)
    playwright.expect(headers.filter(has_text="Symbol")).to_be_in_viewport()
    playwright.expect(headers.filter(has_text="Direction")).to_be_visible()
    page.evaluate("""window.dash_ag_grid.getApi('trades-grid')
        .setGridOption('paginationPageSize', 50)""")

    def expect_empty():
        playwright.expect(headers.filter(has_text="Symbol")).to_be_visible()
        playwright.expect(empty).to_be_visible()
        playwright.expect(symbol).to_have_count(0)
        playwright.expect(direction).to_have_count(0)

    def expect_trade():
        playwright.expect(headers.filter(has_text="Symbol")).to_be_visible()
        playwright.expect(empty).to_be_hidden()
        playwright.expect(symbol).to_have_text("MSFT")
        playwright.expect(symbol).to_be_visible()
        playwright.expect(direction).to_have_text("long")
        playwright.expect(direction).to_be_visible()
        assert (
            page.evaluate("window.dash_ag_grid.getApi('trades-grid').getDisplayedRowCount()") == 1
        )
        assert (
            page.evaluate("window.dash_ag_grid.getApi('trades-grid').paginationGetPageSize()") == 50
        )

    # Stay on Trade history throughout; polling cannot rescue a stale viewport.
    expect_empty()
    for preset in ("1W", "1D", "1W"):
        select_preset(page, "chart", preset)
        if preset == "1D":
            expect_empty()
        else:
            expect_trade()

    page.locator("#link-ranges input").uncheck()
    playwright.expect(page.locator("#history-range-controls")).to_be_visible()
    for preset in ("1D", "1W", "1D"):
        select_preset(page, "history", preset)
        if preset == "1D":
            expect_empty()
        else:
            expect_trade()
        assert checked_preset(page, "chart") == "1W"

    # Relinking applies the chart's wider range to the still-visible empty grid.
    page.locator("#link-ranges input").check()
    expect_trade()
    assert service.calls == snapshot_calls
    assert not errors


def test_linked_ranges_details_positions_and_csv(dashboard):
    page, service, errors = dashboard
    playwright.expect(page.locator("#history-range-controls")).to_be_hidden()
    playwright.expect(page.locator("#pnl-value")).to_have_text("+$1,250.00")
    playwright.expect(page.locator("#trades-grid .ag-pinned-left-cols-container")).to_contain_text(
        "MSFT"
    )
    for preset in ("1W", "1M", "3M", "6M", "YTD", "1Y", "ALL", "1D"):
        select_preset(page, "chart", preset)
        page.wait_for_function(
            "value => document.querySelector('#history-preset input:checked').value === value",
            arg=preset,
        )
    page.locator("#trades-grid .ag-center-cols-container .ag-row").first.click()
    playwright.expect(page.locator("#trade-details")).to_contain_text("MSFT")
    playwright.expect(page.locator("#trade-details")).to_contain_text("fill-1")
    playwright.expect(page.locator("#trade-details")).to_contain_text("Complete execution records")
    columns = page.evaluate("""() => window.dash_ag_grid.getApi('trades-grid')
        .getColumnDefs().map(column => column.headerName)""")
    assert "Strategy" not in columns
    page.locator("#link-ranges input").uncheck()
    playwright.expect(page.locator("#history-range-controls")).to_be_visible()
    select_preset(page, "chart", "1W")
    assert checked_preset(page, "history") == "1D"
    select_preset(page, "history", "1M")
    assert checked_preset(page, "chart") == "1W"
    page.locator("#link-ranges input").check()
    playwright.expect(page.locator("#history-range-controls")).to_be_hidden()
    page.wait_for_function("document.querySelector('#history-preset input:checked').value === '1W'")
    page.locator("#activity-tab").get_by_text("Open now", exact=True).click()
    playwright.expect(
        page.locator("#positions-grid .ag-pinned-left-cols-container")
    ).to_contain_text("AAPL")
    playwright.expect(page.locator("#table-note")).to_contain_text("regardless")
    scrollbar = page.locator("#positions-grid .ag-body-horizontal-scroll").bounding_box()
    last_row = page.locator("#positions-grid .ag-center-cols-container .ag-row").last.bounding_box()
    assert scrollbar["height"] > 0
    assert scrollbar["y"] >= last_row["y"] + last_row["height"] - 1
    with page.expect_download() as download_info:
        page.locator("#export-csv").click()
    download = download_info.value
    assert download.suggested_filename == "alpaca-positions.csv"
    assert "AAPL" in open(download.path(), encoding="utf-8-sig").read()
    page.locator("#activity-tab").get_by_text("Orders", exact=True).click()
    playwright.expect(page.locator("#orders-grid .ag-pinned-left-cols-container")).to_contain_text(
        "MSFT"
    )
    assert not errors


def test_refresh_preserves_grid_and_chart_state_and_recovers(dashboard):
    page, service, errors = dashboard
    playwright.expect(page.locator("#pnl-value")).to_have_text("+$1,250.00")
    page.locator("#table-search").fill("MSFT")
    page.evaluate("""() => {
        const api = window.dash_ag_grid.getApi('trades-grid');
        api.applyColumnState({state: [{colId: 'symbol', sort: 'desc'}]});
        api.setFilterModel({symbol: {filterType: 'text', type: 'contains', filter: 'MSFT'}});

    }""")
    # Exercise real pointer zoom; programmatic relayout is not a Plotly UI edit.
    box = page.locator("#pnl-chart .nsewdrag").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.3)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.7, steps=12)
    page.mouse.up()
    page.evaluate(
        "window.testZoom = document.querySelector('#pnl-chart .js-plotly-plot')"
        ".layout.xaxis.range.slice()"
    )
    page.mouse.move(0, 0)
    service.equity = "101500"
    playwright.expect(page.locator("#account-equity")).to_have_text("$101,500.00", timeout=12000)
    assert page.locator("#table-search").input_value() == "MSFT"
    state = page.evaluate("""() => {
        const api = window.dash_ag_grid.getApi('trades-grid');
        const plot = document.querySelector('#pnl-chart .js-plotly-plot');
        return {sort: api.getColumnState().find(col => col.colId === 'symbol').sort,
            filter: api.getFilterModel().symbol.filter,
            zoom: plot.layout.xaxis.range, expectedZoom: window.testZoom,
            pagination: api.getGridOption('pagination')};
    }""")
    assert state["sort"] == "desc"
    assert state["filter"] == "MSFT"
    assert state["pagination"] is True
    assert state["zoom"] == state["expectedZoom"]
    service.error = "Temporary upstream failure"
    playwright.expect(page.locator("#status-banner")).to_contain_text(
        "last available", timeout=12000
    )
    playwright.expect(page.locator("#account-equity")).to_have_text("$101,500.00")
    service.error = None
    playwright.expect(page.locator("#status-banner")).to_be_hidden(timeout=12000)
    assert not errors


def test_custom_validation_hover_empty_and_mobile(dashboard):
    page, service, errors = dashboard
    playwright.expect(page.locator("#pnl-value")).to_have_text("+$1,250.00")
    playwright.expect(page.locator("h1#account-equity")).to_have_text("$101,250.00")
    playwright.expect(page.locator("#pnl-percent")).to_have_text("(+1.25%)")
    points = page.evaluate("""() => {
        const plot = document.querySelector('#pnl-chart .js-plotly-plot');
        const bounds = plot.getBoundingClientRect(), layout = plot._fullLayout;
        return [1, 3, 5].map(index => {
            const x = layout.xaxis.d2p(plot.data[0].x[index]);
            const nextX = layout.xaxis.d2p(plot.data[0].x[index + 1]);
            const offset = (nextX - x) * 0.35;
            return {x: bounds.x + layout._size.l + x + offset,
                y: bounds.y + layout._size.t + 15, offset,
                equity: plot.data[0].customdata[index][3],
                timestamp: plot.data[0].customdata[index][2]};
        });
    }""")
    # Sparse history must track the nearest point even in gaps above the line.
    for point, amount, percent in zip(
        points,
        ["+$220.00", "+$680.00", "+$900.00"],
        ["(+0.22%)", "(+0.68%)", "(+0.90%)"],
        strict=True,
    ):
        assert point["offset"] > 20
        page.mouse.move(point["x"], point["y"])
        playwright.expect(page.locator("#pnl-period")).to_have_text(point["timestamp"])
        playwright.expect(page.locator("#account-equity")).to_have_text(point["equity"])
        playwright.expect(page.locator("#pnl-value")).to_have_text(amount)
        playwright.expect(page.locator("#pnl-percent")).to_have_text(percent)
    page.mouse.move(0, 0)
    playwright.expect(page.locator("#account-equity")).to_have_text("$101,250.00")
    playwright.expect(page.locator("#pnl-value")).to_have_text("+$1,250.00")
    playwright.expect(page.locator("#pnl-percent")).to_have_text("(+1.25%)")
    select_preset(page, "chart", "CUSTOM")
    playwright.expect(page.locator("#chart-range-error")).not_to_be_empty()
    assert checked_preset(page, "history") == "1D"
    # Incomplete custom dates leave the previously accepted chart in place.
    assert page.evaluate("document.querySelector('#pnl-chart .js-plotly-plot').data.length") == 1
    page.locator("#chart-dates").fill("Jan 02, 2025")
    page.locator("#chart-dates").press("Tab")
    page.locator("#chart-dates-end-date").fill("Jan 03, 2025")
    page.locator("#chart-dates-end-date").press("Tab")
    page.wait_for_function(
        "document.querySelector('#history-preset input:checked').value === 'CUSTOM'"
    )
    playwright.expect(page.locator("#chart-range-error")).to_be_empty()
    playwright.expect(page.locator("#trades-grid")).to_contain_text("No records")
    page.locator("#activity-tab").get_by_text("Open now", exact=True).click()
    playwright.expect(
        page.locator("#positions-grid .ag-pinned-left-cols-container")
    ).to_contain_text("AAPL")
    select_preset(page, "chart", "ALL")
    page.locator("#activity-tab").get_by_text("Trade history", exact=True).click()
    page.screenshot(path="/tmp/alpaca-dashboard-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    playwright.expect(page.locator("#account-equity")).to_be_visible()
    page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.screenshot(path="/tmp/alpaca-dashboard-mobile.png", full_page=True)
    service.empty = True
    page.locator("#activity-tab").get_by_text("Open now", exact=True).click()
    playwright.expect(page.locator("#positions-grid")).to_contain_text("No records", timeout=12000)
    assert not errors


def test_pagination_stays_on_current_page_during_live_refresh(dashboard):
    from datetime import timedelta

    page, service, errors = dashboard
    for index in range(30):
        entry = service.closed - timedelta(seconds=60 - index * 2)
        for side, price, stamp in (
            ("buy", "100", entry),
            ("sell", "101", entry + timedelta(seconds=1)),
        ):
            service.activities.append(
                {
                    "id": f"paged-{index}-{side}",
                    "activity_type": "FILL",
                    "symbol": "NVDA",
                    "qty": "1",
                    "price": price,
                    "side": side,
                    "transaction_time": stamp.isoformat(),
                }
            )
    page.wait_for_function(
        "window.dash_ag_grid.getApi('trades-grid').paginationGetTotalPages() === 2",
        timeout=12000,
    )
    page.locator('#trades-grid [aria-label="Next Page"]').click()
    assert (
        page.evaluate("window.dash_ag_grid.getApi('trades-grid').paginationGetCurrentPage()") == 1
    )
    service.equity = "101600"
    playwright.expect(page.locator("#account-equity")).to_have_text("$101,600.00", timeout=12000)
    assert (
        page.evaluate("window.dash_ag_grid.getApi('trades-grid').paginationGetCurrentPage()") == 1
    )
    assert not errors
