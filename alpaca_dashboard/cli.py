"""Run the dashboard using the current project's credentials and cache."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="alpaca-dashboard", description="Local read-only Alpaca paper account dashboard"
    )
    result.add_argument("--port", type=int, default=8050)
    result.add_argument("--refresh-seconds", type=float, default=5)
    result.add_argument("--env-file", type=Path, default=Path(".env"))
    result.add_argument("--cache-db", type=Path, default=Path("data/dashboard.sqlite"))
    return result


def dashboard_command(args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    if not math.isfinite(args.refresh_seconds) or args.refresh_seconds < 1:
        raise ValueError("--refresh-seconds must be finite and at least 1 second")

    from alpaca_dashboard.client import AlpacaReadOnlyClient
    from alpaca_dashboard.service import DashboardService
    from alpaca_dashboard.ui import create_app

    with AlpacaReadOnlyClient.from_env(env_file=args.env_file, timeout=10) as client:
        service = DashboardService(
            client, cache_path=args.cache_db, refresh_seconds=args.refresh_seconds
        )
        try:
            app = create_app(service)
            service.start()
            print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
            app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
        finally:
            service.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        dashboard_command(args)
        return 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Third-party exceptions may echo transport headers or response bodies.
        print(
            "The dashboard could not start. Check configuration and cache access.", file=sys.stderr
        )
        return 1
