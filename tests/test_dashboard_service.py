from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from alpaca_dashboard.client import AlpacaAPIError
from alpaca_dashboard.service import DashboardService


class Broker:
    timeout = 0.5

    def __init__(self):
        self.account = {"id": "paper-one", "created_at": "2026-01-01T00:00:00Z", "equity": "1000"}
        self.positions = [{"symbol": "SPY", "qty": "1", "avg_entry_price": "100"}]
        self.orders = [
            {
                "id": "entry",
                "status": "filled",
                "submitted_at": "2026-09-29T13:30:00Z",
                "legs": [{"id": "stop", "status": "new"}],
            }
        ]
        self.activities = [{"id": "fill", "activity_type": "FILL", "order_id": "entry"}]
        self.calls = []
        self.fail = False
        self.history = {"timestamp": [1], "equity": [1000], "cashflow": {}}
        self.history_gate = None

    def get_account(self):
        self.calls.append(("account", None))
        if self.fail:
            raise AlpacaAPIError("Private credentials: secret-secret", 503)
        return copy.deepcopy(self.account)

    def get_positions(self):
        self.calls.append(("positions", None))
        return copy.deepcopy(self.positions)

    def get_clock(self):
        return {"is_open": True}

    def get_orders(self, **kwargs):
        self.calls.append(("orders", kwargs))
        if kwargs.get("after_order_id"):
            return []
        return copy.deepcopy(self.orders)

    def get_order(self, order_id):
        self.calls.append(("order", order_id))
        return copy.deepcopy(next(row for row in self.orders if row["id"] == order_id))

    def get_account_activities(self, **kwargs):
        self.calls.append(("activities", kwargs))
        return copy.deepcopy(self.activities)

    def get_portfolio_history(self, start, end, timeframe):
        self.calls.append(("history", (start, end, timeframe)))
        if self.history_gate:
            self.history_gate.wait(2)
        if self.fail:
            raise AlpacaAPIError("Private credentials: secret-secret", 503)
        return copy.deepcopy(self.history)


def make_service(tmp_path, broker=None, **kwargs):
    return DashboardService(broker or Broker(), tmp_path / "dashboard.sqlite", **kwargs)


def window(*, key="1D", live=True, end=None):
    return SimpleNamespace(
        key=key,
        live=live,
        resolution="1Min",
        start=datetime(2026, 9, 30, 13, 30, tzinfo=UTC),
        end=end or datetime(2026, 9, 30, 15, 30, tzinfo=UTC),
    )


def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Background dashboard work did not complete")
        threading.Event().wait(0.01)


def test_snapshot_backfills_then_overlaps_activities_and_refreshes_active_bracket(tmp_path):
    broker = Broker()
    service = make_service(tmp_path, broker)
    service._poll_once()
    first = service.snapshot()
    assert first["history_complete"]
    assert not first["loading"]
    assert first["clock"]["is_open"]
    broker.account["equity"] = "1001"
    broker.orders[0]["legs"][0]["status"] = "filled"
    broker.activities.append({"id": "exit", "activity_type": "FILL"})
    service._poll_once()
    second = service.snapshot()
    assert second["account"]["equity"] == "1001"
    assert len(second["activities"]) == 2
    assert second["orders"][0]["legs"][0]["status"] == "filled"
    assert ("order", "entry") in broker.calls
    after = [
        kwargs["after"]
        for name, kwargs in broker.calls
        if name == "activities" and "after" in kwargs
    ][-1]
    assert after == datetime.fromisoformat(first["as_of"]) - timedelta(days=7)
    service._poll_once()
    assert broker.calls.count(("order", "entry")) == 1
    assert ("orders", {"after_order_id": "entry"}) in broker.calls
    second["account"]["equity"] = "edited by caller"
    assert service.snapshot()["account"]["equity"] == "1001"


def test_legacy_strategy_cache_fields_are_ignored_and_not_saved_again(tmp_path):
    first = make_service(tmp_path)
    first._poll_once()
    legacy = first.snapshot()
    legacy.update(strategy_state={"intents": {"old": {"symbol": "SPY"}}}, strategy_error="old")
    with sqlite3.connect(first.cache_path) as connection:
        connection.execute(
            "UPDATE dashboard_snapshot SET payload=? WHERE id=1", (json.dumps(legacy),)
        )

    second = make_service(tmp_path)
    second._bind_cache("paper-one")
    restored = second.snapshot()
    assert restored["account"] == legacy["account"]
    assert restored["activities"] == legacy["activities"]
    assert "strategy_state" not in restored
    assert "strategy_error" not in restored
    second._poll_once()
    with sqlite3.connect(second.cache_path) as connection:
        saved = json.loads(
            connection.execute("SELECT payload FROM dashboard_snapshot").fetchone()[0]
        )
    assert "strategy_state" not in saved
    assert "strategy_error" not in saved
    assert saved["history_complete"]


def test_service_requires_explicit_start_before_polling(tmp_path):
    broker = Broker()
    service = make_service(tmp_path, broker)
    assert broker.calls == []
    assert service._thread is None
    assert service.snapshot()["loading"]
    service.close()
    assert broker.calls == []


@pytest.mark.parametrize("seconds", [0, -1, True, float("inf"), float("nan")])
def test_invalid_refresh_intervals_are_rejected(tmp_path, seconds):
    with pytest.raises(ValueError, match="finite and positive"):
        make_service(tmp_path, refresh_seconds=seconds)


def test_account_cache_is_verified_before_showing_any_saved_data(tmp_path):
    first = make_service(tmp_path)
    first._poll_once()
    broker = Broker()
    broker.account["id"] = "another-account"
    service = make_service(tmp_path, broker)
    assert service.snapshot()["account"] == {}
    service.start()
    try:
        until(lambda: service.snapshot()["error"] is not None)
        assert "another account" in service.snapshot()["error"]
        assert service.snapshot()["account"] == {}
        assert service.snapshot()["activities"] == []
        assert not any(name == "positions" for name, _ in broker.calls)
    finally:
        service.close()


def test_worker_retains_last_success_on_error_then_recovers(tmp_path):
    broker = Broker()
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(lambda: service.snapshot()["as_of"] is not None)
        first = service.snapshot()
        broker.fail = True
        until(lambda: service.snapshot()["error"] is not None)
        stale = service.snapshot()
        assert stale["account"] == first["account"]
        assert "503" in stale["error"]
        assert "secret" not in json.dumps(stale)
        broker.fail = False
        broker.account["equity"] = "2000"
        until(lambda: service.snapshot()["account"].get("equity") == "2000")
        assert service.snapshot()["error"] is None
    finally:
        service.close()


def test_history_callbacks_never_block_and_live_dates_share_minute_cache(tmp_path):
    broker = Broker()
    broker.history_gate = threading.Event()
    service = make_service(tmp_path, broker)
    service.start()
    thread = service._thread
    service.start()
    assert service._thread is thread
    try:
        until(lambda: service.snapshot()["as_of"] is not None)
        assert service.history(window())["loading"]
        until(lambda: any(name == "history" for name, _ in broker.calls))
        started = time.monotonic()
        assert service.history(window())["data"] is None
        assert service.snapshot()["account"]["id"] == "paper-one"
        assert time.monotonic() - started < 0.1
        broker.history_gate.set()
        until(lambda: service.history(window())["data"] is not None)
        value = service.history(window(end=datetime(2026, 9, 30, 15, 31, tzinfo=UTC)))
        assert value["data"] == broker.history
        assert len([name for name, _ in broker.calls if name == "history"]) == 1
        service.history(window(key="CUSTOM", live=False))
        until(lambda: len([name for name, _ in broker.calls if name == "history"]) == 2)
    finally:
        broker.history_gate.set()
        service.close()


def test_history_errors_keep_cached_curve_and_have_bounded_retry(tmp_path):
    broker = Broker()
    service = make_service(tmp_path, broker)
    service._poll_once()
    service.history(window())
    identity, entry = next(iter(service._histories.items()))
    service._refresh_history(identity, entry)
    previous = service.history(window())
    broker.fail = True
    service._refresh_history(identity, entry)
    current = service.history(window())
    assert current["data"] == previous["data"]
    assert current["as_of"] == previous["as_of"]
    assert "503" in current["error"]
    assert 0 < entry.due - time.monotonic() <= 60


def test_restarted_service_restores_cache_only_after_account_verification(tmp_path):
    first = make_service(tmp_path)
    first._poll_once()
    first.history(window())
    identity, entry = next(iter(first._histories.items()))
    first._refresh_history(identity, entry)
    second = make_service(tmp_path)
    assert second.snapshot()["as_of"] is None
    assert second.history(window())["data"] is None
    second._bind_cache("paper-one")
    assert second.snapshot()["account"]["id"] == "paper-one"
    assert second.snapshot()["loading"]
    assert second.history(window())["data"] == first.client.history


def test_current_account_and_positions_are_published_while_backfill_is_pending(tmp_path):
    broker = Broker()
    entered = threading.Event()
    release = threading.Event()
    original = broker.get_orders

    def pending_orders(**kwargs):
        entered.set()
        release.wait(2)
        return original(**kwargs)

    broker.get_orders = pending_orders
    service = make_service(tmp_path, broker)
    service.start()
    try:
        assert entered.wait(2)
        value = service.snapshot()
        assert value["account"]["equity"] == "1000"
        assert value["positions"][0]["symbol"] == "SPY"
        assert value["loading"]
        assert not value["history_complete"]
        release.set()
        until(lambda: service.snapshot()["history_complete"])
        assert not service.snapshot()["loading"]
    finally:
        release.set()
        service.close()


def test_orders_opened_and_filled_between_polls_are_still_added(tmp_path):
    broker = Broker()
    service = make_service(tmp_path, broker)
    service._poll_once()
    newest = {"id": "instant", "status": "filled", "submitted_at": "2026-09-30T14:00:00Z"}
    broker.get_orders = lambda **_: [newest]
    broker.activities.append({"id": "instant-fill", "activity_type": "FILL", "order_id": "instant"})
    service._poll_once()
    assert service.snapshot()["orders"][0]["id"] == "instant"
    assert service._newest_order_id == "instant"
    assert len(service.snapshot()["activities"]) == 2


def test_history_and_account_broker_requests_are_serialized(tmp_path):
    broker = Broker()
    broker.history_gate = threading.Event()
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(lambda: service.snapshot()["history_complete"])
        service.history(window())
        until(lambda: any(name == "history" for name, _ in broker.calls))
        account_count = sum(name == "account" for name, _ in broker.calls)
        threading.Event().wait(0.06)
        assert sum(name == "account" for name, _ in broker.calls) == account_count
        broker.history_gate.set()
        until(lambda: sum(name == "account" for name, _ in broker.calls) > account_count)
    finally:
        broker.history_gate.set()
        service.close()


def test_closed_market_fetches_initial_and_requested_data_without_recurring_polls(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("alpaca_dashboard.service._HISTORY_INTERVAL", 0.03)
    broker = Broker()
    next_open = datetime.now(UTC) + timedelta(days=1)
    broker.get_clock = lambda: {
        "is_open": False,
        "timestamp": datetime.now(UTC).isoformat(),
        "next_open": next_open.isoformat(),
    }
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(lambda: service.snapshot()["history_complete"])
        assert service.refresh_delay() > 86000
        service.history(window())
        until(lambda: not service.history(window())["loading"])
        threading.Event().wait(0.12)
        assert sum(name == "account" for name, _ in broker.calls) == 1
        assert sum(name == "history" for name, _ in broker.calls) == 1
        assert service.refresh_delay() > 86000
        broker.history_gate = threading.Event()
        custom = window(key="CUSTOM", live=False)
        assert service.history(custom)["loading"]
        until(lambda: sum(name == "history" for name, _ in broker.calls) == 2)
        assert service.refresh_delay() == service.refresh_seconds
        broker.history_gate.set()
        until(lambda: not service.history(custom)["loading"])
        threading.Event().wait(0.08)
        assert sum(name == "account" for name, _ in broker.calls) == 1
        assert sum(name == "history" for name, _ in broker.calls) == 2
        assert service.refresh_delay() > 86000
    finally:
        if broker.history_gate:
            broker.history_gate.set()
        service.close()


def test_worker_resumes_at_open_and_finishes_once_at_close(tmp_path, monkeypatch):
    monkeypatch.setattr("alpaca_dashboard.service._HISTORY_INTERVAL", 60)
    broker = Broker()
    opening = datetime.now(UTC) + timedelta(seconds=0.25)
    closing = opening + timedelta(seconds=0.2)
    next_opening = closing + timedelta(days=1)

    def clock():
        now = datetime.now(UTC)
        return {
            "timestamp": now.isoformat(),
            "is_open": opening <= now < closing,
            "next_open": (opening if now < opening else next_opening).isoformat(),
            "next_close": closing.isoformat(),
        }

    broker.get_clock = clock
    service = make_service(tmp_path, broker, refresh_seconds=0.03)
    service.start()
    try:
        until(lambda: service.snapshot()["history_complete"])
        service.history(window())
        until(lambda: not service.history(window())["loading"])
        assert not service.snapshot()["clock"]["is_open"]
        assert sum(name == "account" for name, _ in broker.calls) == 1
        until(lambda: service.snapshot()["clock"]["is_open"])
        until(lambda: not service.snapshot()["clock"]["is_open"])
        until(lambda: sum(name == "history" for name, _ in broker.calls) == 2)
        final_snapshot = service.snapshot()
        assert datetime.fromisoformat(final_snapshot["as_of"]) >= closing
        account_count = sum(name == "account" for name, _ in broker.calls)
        threading.Event().wait(0.1)
        assert sum(name == "account" for name, _ in broker.calls) == account_count
        assert sum(name == "history" for name, _ in broker.calls) == 2
        assert service.refresh_delay() > 86000
    finally:
        service.close()


def test_failed_on_demand_history_does_not_keep_polling_after_hours(tmp_path):
    broker = Broker()
    broker.get_clock = lambda: {
        "is_open": False,
        "next_open": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(lambda: service.snapshot()["history_complete"])
        broker.fail = True
        service.history(window())
        until(lambda: service.history(window())["error"] is not None)
        threading.Event().wait(0.1)
        assert sum(name == "account" for name, _ in broker.calls) == 1
        assert sum(name == "history" for name, _ in broker.calls) == 1
        assert service.refresh_delay() > 86000
    finally:
        service.close()


def test_inactive_history_refreshes_once_when_revisited_after_close(tmp_path, monkeypatch):
    now = datetime(2026, 10, 5, 19, 59, 59, tzinfo=UTC)
    closing = datetime(2026, 10, 5, 20, tzinfo=UTC)

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr("alpaca_dashboard.service.datetime", ControlledDatetime)
    broker = Broker()
    broker.get_clock = lambda: {
        "is_open": now < closing,
        "timestamp": now.isoformat(),
        "next_close": closing.isoformat(),
        "next_open": "2026-10-06T13:30:00+00:00",
    }
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    selected = window(key="1W", end=now)
    service.start()
    try:
        until(lambda: service.snapshot()["history_complete"])
        service.history(selected)
        until(lambda: not service.history(selected)["loading"])
        with service._lock:
            entry = next(iter(service._histories.values()))
            entry.last_seen = time.monotonic() - 121

        now = closing
        until(lambda: not service.snapshot()["clock"]["is_open"])
        until(lambda: service.refresh_delay() > 60000)
        assert sum(name == "history" for name, _ in broker.calls) == 1

        broker.history["equity"] = [1010]
        broker.history_gate = threading.Event()
        service.history(selected)
        until(lambda: sum(name == "history" for name, _ in broker.calls) == 2)
        assert service.refresh_delay() == service.refresh_seconds
        broker.history_gate.set()
        until(lambda: service.history(selected)["data"]["equity"] == [1010])
        account_count = sum(name == "account" for name, _ in broker.calls)
        threading.Event().wait(0.08)
        assert sum(name == "history" for name, _ in broker.calls) == 2
        assert sum(name == "account" for name, _ in broker.calls) == account_count
        assert service.refresh_delay() > 60000
    finally:
        if broker.history_gate:
            broker.history_gate.set()
        service.close()


def test_initial_backfill_crossing_close_still_fetches_final_snapshot(tmp_path):
    broker = Broker()
    closing = datetime.now(UTC) + timedelta(seconds=0.08)
    next_open = closing + timedelta(days=1)

    def clock():
        now = datetime.now(UTC)
        return {
            "is_open": now < closing,
            "timestamp": now.isoformat(),
            "next_close": closing.isoformat(),
            "next_open": next_open.isoformat(),
        }

    def slow_orders(**kwargs):
        threading.Event().wait(0.1)
        return []

    broker.get_clock = clock
    broker.get_orders = slow_orders
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(
            lambda: (
                service.snapshot()["history_complete"]
                and not service.snapshot()["clock"]["is_open"]
            )
        )
        assert datetime.fromisoformat(service.snapshot()["as_of"]) >= closing
        assert sum(name == "account" for name, _ in broker.calls) == 2
        threading.Event().wait(0.06)
        assert sum(name == "account" for name, _ in broker.calls) == 2
        assert service.refresh_delay() > 86000
    finally:
        service.close()


def test_failed_initial_request_outside_hours_waits_until_calendar_open(tmp_path, monkeypatch):
    closed_at = datetime(2026, 10, 3, 15, tzinfo=UTC)

    class ClosedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return closed_at.astimezone(tz)

    monkeypatch.setattr("alpaca_dashboard.service.datetime", ClosedDatetime)
    broker = Broker()
    broker.fail = True
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(lambda: service.snapshot()["error"] is not None)
        threading.Event().wait(0.1)
        assert broker.calls == [("account", None)]
        next_open = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
        assert service.refresh_delay() == (next_open - closed_at).total_seconds()
    finally:
        service.close()


@pytest.mark.parametrize(
    ("preset", "live", "should_extend"),
    [("1D", True, False), ("CUSTOM", True, False), ("1W", True, True), ("1W", False, False)],
)
def test_final_history_keeps_fixed_boundaries_and_advances_moving_windows(
    tmp_path, preset, live, should_extend
):
    service = make_service(tmp_path)
    service._poll_once()
    before = datetime.now(UTC)
    selected = window(key=preset, live=live, end=before - timedelta(minutes=1))
    service.history(selected)
    identity, entry = next(iter(service._histories.items()))
    service._refresh_history(identity, entry)
    _, (_, requested_end, _) = service.client.calls[-1]
    if should_extend:
        assert before <= requested_end <= datetime.now(UTC)
    else:
        assert requested_end == selected.end


@pytest.mark.parametrize("final_poll_fails", [False, True])
def test_browser_waits_for_final_close_attempt_and_receives_its_result(tmp_path, final_poll_fails):
    broker = Broker()
    closing = datetime.now(UTC) + timedelta(seconds=0.15)
    next_open = closing + timedelta(days=1)
    entered = threading.Event()
    release = threading.Event()
    original_account = broker.get_account

    def gated_account():
        if datetime.now(UTC) >= closing:
            entered.set()
            release.wait(2)
            if final_poll_fails:
                raise AlpacaAPIError("Broker unavailable", 503)
        return original_account()

    def clock():
        now = datetime.now(UTC)
        return {
            "is_open": now < closing,
            "timestamp": now.isoformat(),
            "next_close": closing.isoformat(),
            "next_open": next_open.isoformat(),
        }

    broker.get_account = gated_account
    broker.get_clock = clock
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        assert entered.wait(2)
        delivered = service.snapshot()
        assert delivered["clock"]["is_open"]
        assert service.refresh_delay(delivered) == service.refresh_seconds
        release.set()
        until(lambda: service.refresh_delay() > 86000)
        assert bool(service.snapshot()["error"]) is final_poll_fails
        # The poll can also complete between a callback's snapshot read and
        # cadence calculation; keep ticking so this browser gets the new result.
        assert service.refresh_delay(delivered) == service.refresh_seconds
        assert service.refresh_delay(service.snapshot()) > 86000
    finally:
        release.set()
        service.close()


def test_account_read_before_close_is_updated_when_clock_is_read_after_close(tmp_path, monkeypatch):
    now = datetime(2026, 10, 5, 19, 59, 59, tzinfo=UTC)
    closing = datetime(2026, 10, 5, 20, tzinfo=UTC)

    class AdvancingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr("alpaca_dashboard.service.datetime", AdvancingDatetime)
    broker = Broker()

    def positions_crossing_close():
        nonlocal now
        now = closing + timedelta(seconds=1)
        return broker.positions

    broker.get_positions = positions_crossing_close
    broker.get_clock = lambda: {
        "is_open": False,
        "timestamp": now.isoformat(),
        "next_open": "2026-10-06T13:30:00+00:00",
        "next_close": "2026-10-06T20:00:00+00:00",
    }
    service = make_service(tmp_path, broker, refresh_seconds=0.02)
    service.start()
    try:
        until(lambda: service.snapshot()["as_of"] == now.isoformat())
        until(lambda: service.refresh_delay() > 60000)
        assert sum(name == "account" for name, _ in broker.calls) == 2
    finally:
        service.close()
