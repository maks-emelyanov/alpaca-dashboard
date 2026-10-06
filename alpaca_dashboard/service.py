"""Shared, read-only broker polling and a separate account-bound dashboard cache."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .client import TERMINAL_ORDER_STATUSES, AlpacaAPIError, AlpacaReadOnlyClient
from .market import market_schedule

_NEW_YORK = ZoneInfo("America/New_York")
_ACTIVITY_OVERLAP = timedelta(days=7)
_HISTORY_INTERVAL = 60.0


class _CacheAccountMismatch(ValueError):
    pass


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _safe_error(exc: Exception) -> str:
    # Never expose exception bodies, paths, URLs, or transport messages. Proxies
    # and brokers can echo credential headers in otherwise ordinary errors.
    if isinstance(exc, _CacheAccountMismatch):
        return "Dashboard cache belongs to another account; choose a different cache path."
    if isinstance(exc, AlpacaAPIError) and isinstance(exc.status, int):
        return f"Broker refresh failed (HTTP {exc.status}); retrying."
    if isinstance(exc, sqlite3.Error):
        return "Dashboard cache is unavailable; retrying."
    return "Broker refresh unavailable; retrying."


def _rows_by_id(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise AlpacaAPIError("Broker history contains a missing identifier")
        result[identifier] = row
    return result


def _order_tree(rows: list[dict]):
    for row in rows:
        yield row
        yield from _order_tree(row.get("legs") or [])


def _history_identity(window: Any) -> str:
    # Live end/start timestamps advance with the clock. The local date bounds
    # preserve the one-minute history cadence while the last point updates from
    # the five-second account snapshot. CUSTOM historical bounds remain exact.
    if window.live:
        bounds = [
            value.astimezone(_NEW_YORK).date().isoformat() for value in (window.start, window.end)
        ]
    else:
        bounds = [value.isoformat() for value in (window.start, window.end)]
    value = json.dumps([window.key, window.resolution, *bounds])
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class _History:
    window: Any
    result: dict = field(
        default_factory=lambda: {
            "data": None,
            "as_of": None,
            "error": None,
            "loading": True,
        }
    )
    due: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)
    failures: int = 0
    requested: bool = True


class DashboardService:
    """One background reader shared by every browser session in this process.

    ``history`` is nonblocking: it queues a range and immediately returns its
    last good cached result. Neither browser callbacks nor a second browser can
    start concurrent broker requests. Closing this service does not close the
    caller-owned broker client.
    """

    def __init__(
        self,
        client: AlpacaReadOnlyClient,
        cache_path: str | Path,
        refresh_seconds: float = 5,
    ) -> None:
        if (
            isinstance(refresh_seconds, bool)
            or not math.isfinite(refresh_seconds)
            or refresh_seconds <= 0
        ):
            raise ValueError("Dashboard refresh interval must be finite and positive")
        self.client = client
        self.cache_path = Path(cache_path).expanduser().resolve()
        self.refresh_seconds = float(refresh_seconds)
        self._lock = threading.RLock()
        self._api_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._account_id: str | None = None
        self._backfilled = False
        self._polling = False
        self._next_poll = 0.0
        self._newest_order_id: str | None = None
        self._histories: dict[str, _History] = {}
        self._saved_histories: dict[str, dict] = {}
        self._snapshot: dict[str, Any] = {
            "account": {},
            "positions": [],
            "orders": [],
            "activities": [],
            "clock": {},
            "as_of": None,
            "error": None,
            "loading": True,
            "history_complete": False,
        }
        self._initialize_cache()

    def _initialize_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.cache_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS dashboard_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dashboard_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dashboard_history (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)

    def start(self) -> None:
        """Start once; callbacks may safely invoke this repeatedly."""
        with self._lock:
            if self._thread is not None:
                return
            if self._stop.is_set():
                raise RuntimeError("A closed dashboard service cannot be restarted")
            self._thread = threading.Thread(
                target=self._run,
                name="alpaca-dashboard",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=float(getattr(self.client, "timeout", 30)) + 2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def refresh_delay(self, snapshot: dict[str, Any] | None = None) -> float:
        """Browser cadence: finish queued work, then sleep until the next open."""
        with self._lock:
            if snapshot is not None and snapshot != self._snapshot:
                return self.refresh_seconds
            pending = (
                self._snapshot["loading"]
                or (
                    self._thread is not None
                    and (
                        self._polling or time.monotonic() + self.refresh_seconds >= self._next_poll
                    )
                )
            ) or (
                self._account_id is not None
                and any(
                    entry.requested and time.monotonic() - entry.last_seen <= 120
                    for entry in self._histories.values()
                )
            )
            clock = copy.deepcopy(self._snapshot["clock"])
        if pending:
            return self.refresh_seconds
        return self._market_delay(clock)

    def _market_delay(self, clock: dict) -> float:
        now = datetime.now(UTC)
        schedule = market_schedule(clock, now)
        if schedule.next_transition is None:
            return self.refresh_seconds
        remaining = max(0.01, (schedule.next_transition - now).total_seconds())
        return min(self.refresh_seconds, remaining) if schedule.is_open else remaining

    def history(self, window: Any) -> dict[str, Any]:
        identity = _history_identity(window)
        now = time.monotonic()
        with self._lock:
            entry = self._histories.get(identity)
            if entry is None:
                entry = _History(window)
                saved = self._saved_histories.get(identity)
                if saved is not None:
                    entry.result = copy.deepcopy(saved)
                    entry.result["loading"] = True
                self._histories[identity] = entry
                # Bound per-process memory even when many clients explore ranges.
                if len(self._histories) > 32:
                    oldest = min(self._histories, key=lambda key: self._histories[key].last_seen)
                    if oldest != identity:
                        del self._histories[oldest]
            entry.window = window
            entry.last_seen = now
            result = copy.deepcopy(entry.result)
        self._wake.set()
        return result

    def _bind_cache(self, account_id: str) -> None:
        if self._account_id == account_id:
            return
        if self._account_id is not None:
            raise _CacheAccountMismatch
        with closing(sqlite3.connect(self.cache_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM dashboard_metadata WHERE key='account_id'"
            ).fetchone()
            if row is not None and row[0] != account_id:
                raise _CacheAccountMismatch
            connection.execute(
                "INSERT OR IGNORE INTO dashboard_metadata VALUES ('account_id', ?)",
                (account_id,),
            )
            snapshot = connection.execute(
                "SELECT payload FROM dashboard_snapshot WHERE id=1"
            ).fetchone()
            histories = connection.execute("SELECT key, payload FROM dashboard_history").fetchall()
        with self._lock:
            self._account_id = account_id
            if snapshot:
                cached = json.loads(snapshot[0])
                if cached.get("account", {}).get("id") == account_id:
                    cached.pop("strategy_state", None)
                    cached.pop("strategy_error", None)
                    cached.update(
                        error="Showing cached data while the broker refreshes.",
                        loading=True,
                    )
                    self._snapshot = cached
            self._saved_histories = {key: json.loads(payload) for key, payload in histories}
            for key, entry in self._histories.items():
                if key in self._saved_histories:
                    entry.result = copy.deepcopy(self._saved_histories[key])
                    entry.result["loading"] = True

    def _poll_once(self) -> None:
        """Fetch and publish an atomic snapshot; the worker handles error/backoff."""
        with self._api_lock:
            account = self.client.get_account()
            account_as_of = datetime.now(UTC).isoformat()
            account_id = account.get("id")
            if not isinstance(account_id, str) or not account_id:
                raise AlpacaAPIError("Alpaca account response is missing its identifier")
            self._bind_cache(account_id)
            previous = self.snapshot()
            positions = self.client.get_positions()
            clock = self.client.get_clock()
            if self._stop.is_set():
                return
            if not self._backfilled:
                if previous["as_of"] is None:
                    # Show the account and current positions during the initial
                    # history download, without claiming complete fill coverage.
                    with self._lock:
                        self._snapshot.update(
                            account=account,
                            positions=positions,
                            clock=clock,
                            as_of=account_as_of,
                            loading=True,
                            history_complete=False,
                        )
                orders = self.client.get_orders()
                activities = self.client.get_account_activities()
                newest_order_id = orders[0]["id"] if orders else None
            else:
                # New orders include those opened and filled between two polls.
                fresh_orders = self.client.get_orders(after_order_id=self._newest_order_id)
                newest_order_id = self._newest_order_id
                if fresh_orders:
                    newest_order_id = fresh_orders[-1 if self._newest_order_id else 0]["id"]
                order_map = _rows_by_id(previous["orders"])
                fresh_map = _rows_by_id(fresh_orders)
                for order in previous["orders"]:
                    if self._stop.is_set():
                        return
                    # A filled bracket entry can still have active stop/target legs.
                    active = any(
                        child.get("status") not in TERMINAL_ORDER_STATUSES
                        for child in _order_tree([order])
                    )
                    if active and order["id"] not in fresh_map:
                        fresh_map[order["id"]] = self.client.get_order(order["id"])
                order_map.update(fresh_map)
                orders = sorted(
                    order_map.values(),
                    key=lambda row: (row.get("submitted_at") or "", row["id"]),
                    reverse=True,
                )
                previous_time = _timestamp(previous["as_of"])
                after = previous_time - _ACTIVITY_OVERLAP if previous_time else None
                fresh_activities = self.client.get_account_activities(after=after)
                activity_map = _rows_by_id(previous["activities"])
                activity_map.update(_rows_by_id(fresh_activities))
                activities = list(activity_map.values())
            if self._stop.is_set():
                return
            snapshot = {
                "account": account,
                "positions": positions,
                "orders": orders,
                "activities": activities,
                "clock": clock,
                "as_of": account_as_of,
                "error": None,
                "loading": False,
                "history_complete": True,
            }
            with closing(sqlite3.connect(self.cache_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO dashboard_snapshot VALUES (1, ?) ON CONFLICT(id) "
                    "DO UPDATE SET payload=excluded.payload",
                    (json.dumps(snapshot),),
                )
            with self._lock:
                self._snapshot = snapshot
                self._backfilled = True
                self._newest_order_id = newest_order_id

    def _refresh_history(self, identity: str, entry: _History) -> None:
        with self._lock:
            window = entry.window
        try:
            moving_end = window.live and getattr(
                window, "preset", window.key.split(":")[0]
            ) not in {
                "1D",
                "CUSTOM",
            }
            with self._api_lock:
                data = self.client.get_portfolio_history(
                    window.start,
                    max(window.end, datetime.now(UTC)) if moving_end else window.end,
                    window.resolution,
                )
            result = {
                "data": data,
                "as_of": datetime.now(UTC).isoformat(),
                "error": None,
                "loading": False,
            }
            with closing(sqlite3.connect(self.cache_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO dashboard_history VALUES (?, ?) ON CONFLICT(key) "
                    "DO UPDATE SET payload=excluded.payload",
                    (identity, json.dumps(result)),
                )
            with self._lock:
                entry.result = result
                entry.failures = 0
                entry.requested = False
                entry.due = time.monotonic() + _HISTORY_INTERVAL
        except Exception as exc:
            with self._lock:
                entry.failures += 1
                entry.requested = False
                entry.result.update(error=_safe_error(exc), loading=False)
                entry.due = time.monotonic() + min(
                    60.0,
                    self.refresh_seconds
                    * 2
                    ** min(
                        entry.failures - 1,
                        8,
                    ),
                )

    def _run(self) -> None:
        next_poll = 0.0
        failures = 0
        was_open = False
        while not self._stop.is_set():
            self._wake.clear()
            now = time.monotonic()
            if now >= next_poll:
                with self._lock:
                    self._polling = True
                    previous_clock = copy.deepcopy(self._snapshot["clock"])
                try:
                    self._poll_once()
                    failures = 0
                except Exception as exc:
                    failures += 1
                    with self._lock:
                        self._snapshot.update(error=_safe_error(exc), loading=False)
                with self._lock:
                    clock = copy.deepcopy(self._snapshot["clock"])
                    snapshot_time = _timestamp(self._snapshot["as_of"])
                observed_at = datetime.now(UTC)
                schedule = market_schedule(clock, observed_at)
                close = _timestamp(clock.get("next_close"))
                if close is None or close > observed_at:
                    close = _timestamp(previous_clock.get("next_close"))
                if (
                    not schedule.is_open
                    and snapshot_time is not None
                    and (close is None or close > observed_at)
                ):
                    account_schedule = market_schedule({}, snapshot_time)
                    close = account_schedule.next_transition if account_schedule.is_open else None
                if (
                    not failures
                    and not schedule.is_open
                    and close is not None
                    and snapshot_time is not None
                    and snapshot_time < close <= observed_at
                ):
                    # A slow startup/backfill can finish after the close even
                    # though its account data was read while the market was open.
                    # Fetch the final account snapshot before going to sleep.
                    was_open = True
                    next_poll = 0
                    with self._lock:
                        self._next_poll = next_poll
                        self._polling = False
                    continue
                delay = self._market_delay(clock)
                if failures and schedule.is_open:
                    delay = min(max(60.0, delay), delay * 2 ** min(failures - 1, 8))
                    if schedule.next_transition is not None:
                        delay = min(
                            delay,
                            max(
                                0.01,
                                (schedule.next_transition - datetime.now(UTC)).total_seconds(),
                            ),
                        )
                if was_open and not schedule.is_open:
                    with self._lock:
                        for entry in self._histories.values():
                            # Inactive ranges also need a closing refresh, but
                            # remain queued until a browser selects them again.
                            entry.requested = True
                            entry.due = 0
                was_open = schedule.is_open
                next_poll = time.monotonic() + delay
                with self._lock:
                    self._next_poll = next_poll
                    self._polling = False
            if self._stop.is_set():
                break
            now = time.monotonic()
            with self._lock:
                is_open = market_schedule(self._snapshot["clock"], datetime.now(UTC)).is_open
                pending = [
                    (key, entry)
                    for key, entry in self._histories.items()
                    if (entry.requested or (is_open and entry.due <= now))
                    and now - entry.last_seen <= 120
                ]
                ready = self._account_id is not None
            if ready and pending:
                identity, entry = min(pending, key=lambda item: item[1].due)
                self._refresh_history(identity, entry)
                continue
            wake_at = next_poll
            if ready and is_open:
                with self._lock:
                    for entry in self._histories.values():
                        if now - entry.last_seen <= 120:
                            wake_at = min(wake_at, entry.due)
            self._wake.wait(timeout=max(0.01, wake_at - time.monotonic()))
