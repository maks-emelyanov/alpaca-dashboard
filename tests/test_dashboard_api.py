from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from http_helpers import Response, client

from alpaca_dashboard.client import AlpacaAPIError


def query(opener, number=0):
    return parse_qs(urlsplit(opener.calls[number][0].full_url).query)


def test_historical_orders_paginate_by_id_and_keep_nested_legs():
    rows = [{"id": "new", "legs": [{"id": "stop"}]}, {"id": "old"}]
    api, opener = client(Response(rows), Response([]))
    assert api.get_orders(page_size=2) == rows
    assert query(opener)["status"] == ["all"]
    assert query(opener, 1)["before_order_id"] == ["old"]
    assert all(request.get_method() == "GET" for request, _ in opener.calls)


def test_incremental_orders_paginate_forward_without_timestamp_bounds():
    api, opener = client(Response([{"id": "next"}]), Response([{"id": "last"}]), Response([]))
    assert api.get_orders(after_order_id="previous", page_size=1) == [
        {"id": "next"},
        {"id": "last"},
    ]
    assert [query(opener, index)["after_order_id"] for index in range(3)] == [
        ["previous"],
        ["next"],
        ["last"],
    ]
    assert query(opener)["direction"] == ["asc"]
    assert "before_order_id" not in query(opener, 1)


def test_activity_pages_overlap_without_duplicate_executions():
    api, opener = client(
        Response([{"id": "a", "activity_type": "FILL"}, {"id": "b"}]),
        Response([{"id": "b"}, {"id": "c", "activity_type": "DIV"}]),
        Response([]),
    )
    rows = api.get_account_activities(after="2026-01-01", until="2026-02-01", page_size=2)
    assert [row["id"] for row in rows] == ["a", "b", "c"]
    assert query(opener) == {
        "after": ["2026-01-01"],
        "until": ["2026-02-01"],
        "direction": ["asc"],
        "page_size": ["2"],
    }
    assert query(opener, 1)["page_token"] == ["b"]
    assert query(opener, 2)["page_token"] == ["c"]
    assert all(request.get_method() == "GET" for request, _ in opener.calls)


def test_stalled_activity_pagination_raises_instead_of_marking_history_complete():
    api, opener = client(Response([{"id": "a"}]), Response([{"id": "a"}]))
    with pytest.raises(AlpacaAPIError, match="stalled"):
        api.get_account_activities(page_size=1)
    assert len(opener.calls) == 2


@pytest.mark.parametrize("rows", [[{}], [{"id": 1}], [{"id": "a"}, {"id": "b"}]])
def test_malformed_activity_pages_fail(rows):
    api, _ = client(Response(rows))
    with pytest.raises(AlpacaAPIError):
        api.get_account_activities(page_size=1)


@pytest.mark.parametrize("page_size", [0, 101, True, 1.5])
def test_activity_page_size_validation(page_size):
    api, opener = client()
    with pytest.raises(ValueError, match="page size"):
        api.get_account_activities(page_size=page_size)
    assert not opener.calls


def test_portfolio_history_has_explicit_dates_and_cashflow_adjustments():
    api, opener = client(Response({"timestamp": [], "cashflow": {}}))
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)
    assert api.get_portfolio_history(start, end, "1Min") == {"timestamp": [], "cashflow": {}}
    assert query(opener) == {
        "start": [start.isoformat()],
        "end": [end.isoformat()],
        "timeframe": ["1Min"],
        "pnl_reset": ["no_reset"],
        "cashflow_types": ["ALL"],
        "intraday_reporting": ["market_hours"],
    }
    assert opener.calls[0][0].get_method() == "GET"


def test_portfolio_history_rejects_naive_and_reversed_ranges_without_request():
    api, opener = client()
    date = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        api.get_portfolio_history(date.replace(tzinfo=None), date, "1D")
    with pytest.raises(ValueError, match="precede"):
        api.get_portfolio_history(date, date, "1D")
    assert not opener.calls


def test_last_partial_activity_page_can_contain_an_overlapping_execution():
    api, _ = client(Response([{"id": "a"}, {"id": "b"}]), Response([{"id": "b"}]))
    assert api.get_account_activities(page_size=2) == [{"id": "a"}, {"id": "b"}]
