"""GET-only Alpaca paper-account client with destination-project credentials."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
from collections.abc import Mapping
from datetime import datetime
from http import HTTPStatus
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
_KEY_NAMES = ("ALPACA_API_KEY_ID", "ALPACA_API_KEY", "APCA_API_KEY_ID")
_SECRET_NAMES = (
    "ALPACA_API_SECRET_KEY",
    "ALPACA_API_SECRET",
    "ALPACA_SECRET_KEY",
    "APCA_API_SECRET_KEY",
)
_URL_NAMES = ("ALPACA_BASE_URL", "APCA_API_BASE_URL", "ALPACA_PAPER_BASE_URL")
_ENV_NAMES = frozenset((*_KEY_NAMES, *_SECRET_NAMES, *_URL_NAMES))
_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class AlpacaAPIError(RuntimeError):
    """Sanitized API failure; ``status=None`` indicates a transport or data error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise copies API key headers into the redirected request.
        return None


def _read_dotenv(path: str | Path) -> dict[str, str]:
    """Read only Alpaca settings, without evaluation or global environment changes."""
    try:
        contents = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for number, line in enumerate(contents.splitlines(), 1):
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw = line.partition("=")
        name = name.strip()
        if name not in _ENV_NAMES:
            continue
        if not separator:
            raise ValueError(f"Invalid Alpaca setting in .env at line {number}")
        raw = raw.strip()
        try:
            if raw.startswith(("'", '"')):
                parts = shlex.split(raw, comments=True, posix=True)
                if len(parts) != 1:
                    raise ValueError
                value = parts[0]
            else:
                value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        except ValueError:
            raise ValueError(f"Invalid Alpaca setting in .env at line {number}") from None
        values[name] = value
    return values


def _setting(names: tuple[str, ...], source: Mapping[str, str]) -> str | None:
    for name in names:
        if name in source:
            return source[name]
    return None


def _component(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value in (".", ".."):
        raise ValueError("A nonempty Alpaca resource identifier is required")
    return quote(value, safe="")


class AlpacaReadOnlyClient:
    """Synchronous read-only client restricted to Alpaca's HTTPS paper origin.

    Construction does not make requests or start a background reader.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = ALPACA_PAPER_BASE_URL,
        timeout: float = 30.0,
        opener: Any = None,
    ) -> None:
        if not isinstance(base_url, str) or base_url.rstrip("/") != ALPACA_PAPER_BASE_URL:
            raise ValueError(
                "Only https://paper-api.alpaca.markets is allowed for the paper dashboard"
            )
        for credential in (api_key, api_secret):
            if (
                not isinstance(credential, str)
                or not credential.strip()
                or any(ord(character) < 33 or ord(character) > 126 for character in credential)
            ):
                raise ValueError("Alpaca API key and secret must be nonempty printable credentials")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Alpaca request timeout must be finite and positive")
        self.timeout = timeout
        self._api_key = api_key
        self._api_secret = api_secret
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    @property
    def base_url(self) -> str:
        return ALPACA_PAPER_BASE_URL

    @classmethod
    def from_env(
        cls, *, env_file: str | Path = Path(".env"), timeout: float = 30.0
    ) -> AlpacaReadOnlyClient:
        values = _read_dotenv(env_file)
        # Select credentials atomically. A partial file must never silently use
        # another project's shell key or secret, nor its base URL setting.
        source = (
            values if any(name in values for name in (*_KEY_NAMES, *_SECRET_NAMES)) else os.environ
        )
        key = _setting(_KEY_NAMES, source)
        secret = _setting(_SECRET_NAMES, source)
        if not key or not secret:
            location = "selected .env file" if source is values else "environment or .env"
            raise ValueError(f"Missing Alpaca API key or secret in the {location}")
        return cls(
            key,
            secret,
            base_url=_setting(_URL_NAMES, source) or ALPACA_PAPER_BASE_URL,
            timeout=timeout,
        )

    def __enter__(self) -> AlpacaReadOnlyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._opener.close()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(
            url,
            method="GET",
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._api_secret,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "alpaca-dashboard/0.1 AlpacaReadOnlyClient",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                if status < 200 or status >= 300:
                    raise AlpacaAPIError(f"Alpaca paper request failed (HTTP {status})", status)
                if status == 204:
                    return None
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            try:
                reason = HTTPStatus(status).phrase
            except ValueError:
                reason = "Request failed"
            # Neither upstream bodies nor transport exceptions are safe to log:
            # servers/proxies can echo the request's credentials in either one.
            raise AlpacaAPIError(
                f"Alpaca paper request failed (HTTP {status}: {reason})", status
            ) from None
        except (URLError, OSError, HTTPException):
            raise AlpacaAPIError("Alpaca paper request failed during transport") from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AlpacaAPIError("Alpaca paper response exceeded the size limit")
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise AlpacaAPIError("Alpaca paper returned an invalid JSON response") from None

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AlpacaAPIError("Alpaca paper returned an invalid object response")
        return value

    @staticmethod
    def _records(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise AlpacaAPIError("Alpaca paper returned an invalid list response")
        return value

    def get_account(self) -> dict[str, Any]:
        return self._object(self._get("/v2/account"))

    def get_clock(self) -> dict[str, Any]:
        return self._object(self._get("/v2/clock"))

    def get_positions(self) -> list[dict[str, Any]]:
        return self._records(self._get("/v2/positions"))

    def get_open_orders(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        """Read every open order with exit legs, rejecting stalled pagination.

        Alpaca's exclusive order-ID cursor avoids losing orders that share the
        same submission timestamp at a page boundary.
        """
        return self.get_orders(status="open", page_size=page_size)

    def get_orders(
        self,
        *,
        status: str = "all",
        page_size: int = 500,
        after_order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read order history using exclusive ID cursors, retaining nested legs.

        ``after_order_id`` reads newer submissions in ascending order, allowing
        dashboard polling to fetch only newly created orders without timestamp
        boundary losses. Previously open orders must also be refreshed by ID.
        """
        if status not in {"all", "open", "closed"}:
            raise ValueError("Alpaca order status must be all, open, or closed")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 500
        ):
            raise ValueError("Alpaca order page size must be an integer from 1 to 500")
        params: dict[str, Any] = {
            "status": status,
            "nested": "true",
            "direction": "asc" if after_order_id else "desc",
            "limit": page_size,
        }
        if after_order_id is not None:
            _component(after_order_id)
            params["after_order_id"] = after_order_id
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(10000):
            page = self._records(self._get("/v2/orders", params=params))
            if len(page) > page_size:
                raise AlpacaAPIError("Alpaca order page exceeded the requested limit")
            for row in page:
                order_id = row.get("id")
                if not isinstance(order_id, str) or not order_id or order_id in seen:
                    raise AlpacaAPIError("Alpaca order pagination returned missing or repeated IDs")
                seen.add(order_id)
                records.append(row)
            if len(page) < page_size:
                return records
            cursor = "after_order_id" if after_order_id else "before_order_id"
            params[cursor] = page[-1]["id"]
        raise AlpacaAPIError("Alpaca order pagination exceeded its safety limit")

    def get_account_activities(
        self,
        *,
        after: str | datetime | None = None,
        until: str | datetime | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Read all activity types, deduplicating overlapping execution pages.

        See https://docs.alpaca.markets/us/reference/getaccountactivities-2.
        No activity type filter is sent, preserving corporate actions and fees.
        """
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("Alpaca activity page size must be an integer from 1 to 100")
        params: dict[str, Any] = {"direction": "asc", "page_size": page_size}
        for name, value in (("after", after), ("until", until)):
            if value is not None:
                params[name] = value.isoformat() if isinstance(value, datetime) else value
        records: dict[str, dict[str, Any]] = {}
        cursors: set[str] = set()
        for _ in range(10000):
            page = self._records(self._get("/v2/account/activities", params=params))
            if len(page) > page_size:
                raise AlpacaAPIError("Alpaca activity page exceeded the requested limit")
            previous_size = len(records)
            for row in page:
                activity_id = row.get("id")
                if not isinstance(activity_id, str) or not activity_id:
                    raise AlpacaAPIError("Alpaca activity pagination returned a missing ID")
                records[activity_id] = row
            if len(page) < page_size:
                return list(records.values())
            if previous_size == len(records):
                raise AlpacaAPIError("Alpaca activity pagination stalled")
            cursor = page[-1]["id"]
            if cursor in cursors:
                raise AlpacaAPIError("Alpaca activity pagination stalled")
            cursors.add(cursor)
            params["page_token"] = cursor
        raise AlpacaAPIError("Alpaca activity pagination exceeded its safety limit")

    def get_portfolio_history(
        self, start: datetime, end: datetime, timeframe: str
    ) -> dict[str, Any]:
        """Read equity history and all typed cash-flow adjustment buckets.

        See https://docs.alpaca.markets/us/reference/getaccountportfoliohistory-1.
        Callers select external funding buckets when calculating account P&L;
        dividends, fees, and interest must remain part of account performance.
        """
        if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
            raise ValueError("Portfolio history timestamps must include a timezone")
        if start >= end:
            raise ValueError("Portfolio history start must precede end")
        if timeframe not in {"1Min", "5Min", "15Min", "1H", "1D"}:
            raise ValueError("Unsupported Alpaca portfolio history resolution")
        return self._object(
            self._get(
                "/v2/account/portfolio/history",
                params={
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timeframe": timeframe,
                    "pnl_reset": "no_reset",
                    "cashflow_types": "ALL",
                    "intraday_reporting": "market_hours",
                },
            )
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._object(
            self._get(f"/v2/orders/{_component(order_id)}", params={"nested": "true"})
        )
