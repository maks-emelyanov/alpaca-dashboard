"""Reconstruct gross trade lifecycles from executions, never cumulative orders.

All arithmetic stays in Decimal. Strings cross the presentation boundary, so
neither SQLite nor a browser silently rounds fractional quantities or money.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from .models import ZERO, decimal_text, number, timestamp

SECURITY_EVENTS = {
    "ACATS",
    "FOPT",
    "JNLS",
    "MA",
    "NC",
    "OPASN",
    "OPEXP",
    "OPXRC",
    "REORG",
    "SC",
    "SSO",
    "SSP",
    "SPIN",
    "SPLIT",
}
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}
# Alpaca's activity-side enum is broader than order submission buy/sell.
# Source: alpacahq/alpaca-docs, oas/broker/openapi.yaml (OrderSide).
# Cross/undisclosed sides have no reliable cash-flow direction and remain incomplete.
SIDE_SIGN = {
    "buy": 1,
    "buy_minus": 1,
    "sell": -1,
    "sell_plus": -1,
    "sell_short": -1,
    "sell_short_exempt": -1,
}


def _identity(row: dict) -> str:
    return str(
        row.get("id")
        or hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:24]
    )


def _flatten(orders: Iterable[dict], parent_id: str | None = None) -> list[dict]:
    result: dict[str, dict] = {}
    for order in orders:
        row = {**order, "id": _identity(order), "parent_id": parent_id or order.get("parent_id")}
        old = result.get(row["id"])
        if old and old.get("parent_id"):
            row["parent_id"] = old["parent_id"]
        result[row["id"]] = row
        for leg in _flatten(order.get("legs") or [], row["id"]):
            result[leg["id"]] = leg
    return list(result.values())


def _supported(row: dict) -> bool:
    asset_class = row.get("asset_class")
    if asset_class and asset_class != "us_equity":
        return False
    symbol = str(row.get("symbol") or "")
    return bool(symbol) and "/" not in symbol and not re.search(r"\d{6}[CP]\d{8}$", symbol)


def _signed_position(position: dict) -> Decimal:
    qty = number(position.get("qty")) or ZERO
    return -abs(qty) if position.get("side") == "short" else qty


def order_rows(orders: Iterable[dict]) -> list[dict]:
    rows = []
    for order in _flatten(orders):
        row = {
            field: order.get(field)
            for field in (
                "id",
                "submitted_at",
                "symbol",
                "side",
                "type",
                "qty",
                "filled_qty",
                "filled_avg_price",
                "limit_price",
                "stop_price",
                "status",
                "parent_id",
                "client_order_id",
                "order_class",
                "asset_class",
                "time_in_force",
            )
        }
        rows.append(row)
    return sorted(rows, key=lambda row: (row.get("submitted_at") or "", row["id"]), reverse=True)


def position_rows(
    positions: Iterable[dict],
    orders: Iterable[dict],
) -> list[dict]:
    # Basis and current returns come from the broker, not inferred fills.
    flat = _flatten(orders)
    rows = []
    for position in positions:
        symbol = position.get("symbol")
        qty = _signed_position(position)
        exits = [
            order
            for order in flat
            if order.get("symbol") == symbol
            and order.get("side") == ("buy" if qty < 0 else "sell")
            and order.get("status") not in TERMINAL
        ]
        stops = sorted({str(order["stop_price"]) for order in exits if order.get("stop_price")})
        targets = sorted(
            {
                str(order["limit_price"])
                for order in exits
                if order.get("type") == "limit" and order.get("limit_price")
            }
        )
        row = {
            field: position.get(field)
            for field in (
                "symbol",
                "side",
                "qty",
                "qty_available",
                "avg_entry_price",
                "current_price",
                "cost_basis",
                "market_value",
                "unrealized_pl",
                "unrealized_intraday_pl",
                "asset_class",
            )
        }
        row.update(
            id=str(position.get("asset_id") or symbol),
            unrealized_pct=decimal_text(_percent(position.get("unrealized_plpc"))),
            stop_price=", ".join(stops) or None,
            target_price=", ".join(targets) or None,
            linked_orders=[order["id"] for order in exits],
        )
        rows.append(row)
    return sorted(rows, key=lambda row: row["symbol"] or "")


def _percent(value: Any) -> Decimal | None:
    parsed = number(value)
    return parsed * 100 if parsed is not None else None


def _fill_time(fill: dict) -> datetime:
    return timestamp(fill.get("transaction_time") or fill.get("timestamp"))


def _new_lifecycle(fill: dict, quantity: Decimal, price: Decimal, part: int) -> dict:
    return {
        "id": f"trade:{fill['symbol']}:{_identity(fill)}:{part}",
        "symbol": fill["symbol"],
        "direction": "long" if quantity > 0 else "short",
        "opened_at": _fill_time(fill),
        "closed_at": None,
        "net": ZERO,
        "entry_qty": ZERO,
        "exit_qty": ZERO,
        "entry_value": ZERO,
        "exit_value": ZERO,
        "cash": ZERO,
        "details": [],
        "supported": _supported(fill),
    }


def _allocate(
    lifecycle: dict,
    fill: dict,
    quantity: Decimal,
    price: Decimal,
) -> None:
    entry = (quantity > 0) == (lifecycle["direction"] == "long")
    prefix = "entry" if entry else "exit"
    amount = abs(quantity)
    lifecycle[f"{prefix}_qty"] += amount
    lifecycle[f"{prefix}_value"] += amount * price
    lifecycle["cash"] -= quantity * price
    lifecycle["net"] += quantity
    lifecycle["supported"] &= _supported(fill)
    lifecycle["details"].append(
        {
            **fill,
            "id": _identity(fill),
            "qty": str(amount),
            "original_qty": fill.get("qty"),
            "allocation": prefix,
            "transaction_time": _fill_time(fill).isoformat(),
            "price": str(price),
        }
    )
    if lifecycle["net"] == 0:
        lifecycle["closed_at"] = _fill_time(fill)


def _render(lifecycle: dict, reason: str | None) -> dict:
    completed = lifecycle["closed_at"] is not None
    if not lifecycle["supported"]:
        reason = "Unsupported instrument; calculated returns unavailable"
    good = completed and not reason
    entry_value, exit_value = lifecycle["entry_value"], lifecycle["exit_value"]
    entry_qty, exit_qty = lifecycle["entry_qty"], lifecycle["exit_qty"]
    return {
        "id": lifecycle["id"],
        "symbol": lifecycle["symbol"],
        "direction": lifecycle["direction"],
        "quantity": str(entry_qty),
        "opened_at": lifecycle["opened_at"].isoformat(),
        "closed_at": lifecycle["closed_at"].isoformat() if completed else None,
        "entry_price": decimal_text(entry_value / entry_qty if entry_qty else None),
        "exit_price": decimal_text(exit_value / exit_qty if exit_qty else None),
        "duration_seconds": int((lifecycle["closed_at"] - lifecycle["opened_at"]).total_seconds())
        if completed
        else None,
        "realized_pnl": decimal_text(lifecycle["cash"] if good else None),
        "realized_pct": decimal_text(lifecycle["cash"] / entry_value * 100)
        if good and entry_value
        else None,
        "status": "Completed" if good else f"Incomplete: {reason or 'open lifecycle'}",
        "details": lifecycle["details"],
    }


def trade_rows(
    activities: Iterable[dict],
    positions: Iterable[dict],
    orders: Iterable[dict],
    *,
    history_complete: bool = True,
) -> list[dict]:
    """Build zero-to-zero lifecycles before applying any UI timeframe filtering.

    Reversal fills are allocated between two lifecycles. Incomplete execution
    coverage and quantity-changing events suppress calculated returns rather
    than silently manufacturing a cost basis.
    """
    activities = list({_identity(item): item for item in activities}.values())
    positions = list(positions)
    order_map = {item["id"]: item for item in _flatten(orders)}
    actual = {item["symbol"]: _signed_position(item) for item in positions}
    classes = {item["symbol"]: item.get("asset_class") for item in positions}
    classes.update(
        {
            item["symbol"]: item.get("asset_class")
            for item in order_map.values()
            if item.get("asset_class") and item.get("symbol")
        }
    )
    problems: dict[str, str] = {}
    fills = []
    invalid = []
    for item in activities:
        kind = str(item.get("activity_type") or "").upper()
        symbol = item.get("symbol")
        if kind in SECURITY_EVENTS and symbol:
            problems[symbol] = "Security transfer or corporate action requires adjusted history"
        if kind != "FILL":
            continue
        if symbol:
            item = {**item, "asset_class": item.get("asset_class") or classes.get(symbol)}
        try:
            _fill_time(item)
            quantity, price = number(item.get("qty")), number(item.get("price"))
            if not symbol or quantity is None or quantity <= 0 or price is None or price < 0:
                raise ValueError
            if item.get("side") not in SIDE_SIGN:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            problems[symbol or "Unknown"] = "Malformed execution record"
            invalid.append(item)
            continue
        fills.append(item)
    fills.sort(key=lambda item: (_fill_time(item), _identity(item)))
    net = defaultdict(lambda: ZERO)
    current = {}
    completed = []
    for fill in fills:
        symbol = fill["symbol"]
        quantity = number(fill["qty"]) * SIDE_SIGN[fill["side"]]
        price = number(fill["price"])
        net[symbol] += quantity
        lifecycle = current.get(symbol)
        part = 0
        if lifecycle and lifecycle["net"] * quantity < 0:
            closing = min(abs(quantity), abs(lifecycle["net"])) * (1 if quantity > 0 else -1)
            _allocate(lifecycle, fill, closing, price)
            quantity -= closing
            if lifecycle["net"] == 0:
                completed.append(lifecycle)
                current.pop(symbol)
                lifecycle = None
                part = 1
        if quantity:
            if lifecycle is None:
                lifecycle = _new_lifecycle(fill, quantity, price, part)
                current[symbol] = lifecycle
            _allocate(lifecycle, fill, quantity, price)
    for symbol in net.keys() | actual.keys():
        if net[symbol] != actual.get(symbol, ZERO):
            problems.setdefault(
                symbol, "Execution quantities do not reconcile with current holdings"
            )
    rows = []
    for lifecycle in [*completed, *current.values()]:
        reason = problems.get(lifecycle["symbol"])
        if not history_complete:
            reason = "Account execution history is incomplete"
        if lifecycle["closed_at"] is not None or reason or not lifecycle["supported"]:
            rows.append(_render(lifecycle, reason))
    for fill in invalid:
        rows.append(
            {
                "id": f"incomplete:{_identity(fill)}",
                "symbol": fill.get("symbol") or "Unknown",
                "direction": None,
                "quantity": fill.get("qty"),
                "opened_at": None,
                "closed_at": None,
                "entry_price": None,
                "exit_price": None,
                "duration_seconds": None,
                "realized_pnl": None,
                "realized_pct": None,
                "status": "Incomplete: malformed execution record",
                "details": [fill],
            }
        )
    return sorted(
        rows,
        key=lambda row: (row.get("closed_at") or row.get("opened_at") or "", row["id"]),
        reverse=True,
    )
