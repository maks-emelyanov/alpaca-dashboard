"""Synthetic, credential-free dashboard data for browser integration tests."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from alpaca_dashboard.sessions import NEW_YORK, SessionCalendar


class DemoDashboardService:
    refresh_seconds = 5

    def __init__(self, *, previous_session=False, refresh_seconds=5):
        self.refresh_seconds = refresh_seconds
        self.calls = 0
        self.history_calls = []
        self.error = None
        self.empty = False
        self.equity = "101250"
        now = datetime.now(UTC)
        calendar = SessionCalendar()
        day = now.astimezone(NEW_YORK).date()
        while not (session := calendar.session(day)) or session.open > now:
            day -= timedelta(days=1)
        if previous_session:
            day -= timedelta(days=1)
            while not (session := calendar.session(day)):
                day -= timedelta(days=1)
        self.opened = session.open + timedelta(minutes=1)
        self.closed = min(session.close, self.opened + timedelta(minutes=12))
        self.orders = [
            {
                "id": "entry-msft",
                "symbol": "MSFT",
                "asset_class": "us_equity",
                "side": "buy",
                "type": "limit",
                "qty": "10",
                "filled_qty": "10",
                "filled_avg_price": "400",
                "limit_price": "400",
                "status": "filled",
                "submitted_at": self.opened.isoformat(),
                "filled_at": self.opened.isoformat(),
                "legs": [],
                "client_order_id": "fixture-entry",
            },
            {
                "id": "exit-msft",
                "symbol": "MSFT",
                "asset_class": "us_equity",
                "side": "sell",
                "type": "limit",
                "qty": "10",
                "filled_qty": "10",
                "filled_avg_price": "405",
                "limit_price": "405",
                "status": "filled",
                "submitted_at": self.closed.isoformat(),
                "filled_at": self.closed.isoformat(),
                "legs": [],
            },
            {
                "id": "entry-aapl",
                "symbol": "AAPL",
                "asset_class": "us_equity",
                "side": "buy",
                "type": "limit",
                "qty": "10",
                "filled_qty": "10",
                "filled_avg_price": "140",
                "status": "filled",
                "submitted_at": self.opened.isoformat(),
                "filled_at": self.opened.isoformat(),
                "legs": [],
            },
        ]
        self.activities = [
            {
                "id": "fill-1",
                "activity_type": "FILL",
                "symbol": "MSFT",
                "qty": "10",
                "price": "400",
                "side": "buy",
                "transaction_time": self.opened.isoformat(),
                "order_id": "entry-msft",
            },
            {
                "id": "fill-2",
                "activity_type": "FILL",
                "symbol": "MSFT",
                "qty": "10",
                "price": "405",
                "side": "sell",
                "transaction_time": self.closed.isoformat(),
                "order_id": "exit-msft",
            },
            {
                "id": "fill-3",
                "activity_type": "FILL",
                "symbol": "AAPL",
                "qty": "10",
                "price": "140",
                "side": "buy",
                "transaction_time": self.opened.isoformat(),
                "order_id": "entry-aapl",
            },
        ]

    def snapshot(self):
        self.calls += 1
        now = datetime.now(UTC)
        return deepcopy(
            {
                "account": {
                    "id": "fixture-account",
                    "created_at": "2024-01-02T14:30:00Z",
                    "equity": self.equity,
                    "cash": "99750",
                    "buying_power": "199500",
                    "currency": "USD",
                },
                "positions": []
                if self.empty
                else [
                    {
                        "asset_id": "aapl",
                        "symbol": "AAPL",
                        "asset_class": "us_equity",
                        "side": "long",
                        "qty": "10",
                        "qty_available": "10",
                        "avg_entry_price": "140",
                        "current_price": "150",
                        "cost_basis": "1400",
                        "market_value": "1500",
                        "unrealized_pl": "100",
                        "unrealized_plpc": "0.07142857",
                        "unrealized_intraday_pl": "20",
                    }
                ],
                "orders": [] if self.empty else self.orders,
                "activities": [] if self.empty else self.activities,
                "as_of": now.isoformat(),
                "error": self.error,
                "loading": False,
                "history_complete": True,
                "clock": {"is_open": True, "timestamp": now.isoformat()},
            }
        )

    def history(self, window):
        self.history_calls.append(window)
        start = window.start
        end = min(window.end, datetime.now(UTC))
        if end <= start:
            end = start + timedelta(hours=1)
        timestamps = [int((start + (end - start) * n / 6).timestamp()) for n in range(7)]
        values = [100000, 100220, 100100, 100680, 100520, 100900, float(self.equity)]
        if self.empty:
            timestamps, values = [], []
        return {
            "data": {
                "timestamp": timestamps,
                "equity": values,
                "base_value": 100000,
                "base_value_asof": (start.date() - timedelta(days=1)).isoformat(),
                "profit_loss": [value - 100000 for value in values],
                "profit_loss_pct": [(value - 100000) / 100000 for value in values],
                "timeframe": window.resolution,
                "cashflow": {kind: [0] * len(values) for kind in ("CSD", "CSW", "ACATC", "JNLC")},
            },
            "as_of": datetime.now(UTC).isoformat(),
            "error": self.error,
            "loading": False,
        }
