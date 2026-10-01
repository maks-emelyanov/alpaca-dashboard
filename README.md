# Alpaca Dashboard

A reusable, local dashboard for an entire Alpaca paper account, built with Dash, Plotly, and Dash AG Grid Community. Supports Python 3.12–3.14. The dashboard makes GET-only broker requests and runs independently of any trading process.

## Quickstart

You need Python 3.12 or newer, `uv`, and an Alpaca **paper** account. Clone this repository and install its locked dependencies:

```bash
git clone https://github.com/maks-emelyanov/alpaca-dashboard.git
cd alpaca-dashboard
uv sync --frozen --no-dev
cp .env.example .env
```

Edit `.env` and replace both placeholder values with your paper API key and secret, then launch:

```bash
uv run --frozen --no-dev alpaca-dashboard
```

Open <http://127.0.0.1:8050>. Stop the dashboard with `Ctrl+C`. Initial history backfill can take longer for large accounts; account and position data appear while history loads.

The local UI has no login and displays account data. Run it on a trusted machine, keep the server bound to loopback, and treat the cache and CSV exports as private. See [SECURITY.md](SECURITY.md) for the security scope.

## Use from another project

From the destination project's root directory:

```bash
uv add --editable /path/to/alpaca-dashboard
uv run alpaca-dashboard
# Open http://127.0.0.1:8050
```

For sibling projects, use `uv add --editable ../alpaca-dashboard`. An editable dependency immediately picks up changes to this checkout; restart the dashboard after Python changes. A built wheel can also be installed with `uv add /path/to/alpaca_dashboard-0.1.0-py3-none-any.whl`.

Add that destination project's paper credentials to its git-ignored `.env`:

```dotenv
ALPACA_API_KEY=your-paper-key
ALPACA_API_SECRET=your-paper-secret
```

This repository's ignore rules do not apply to another project; add `.env`, the dashboard cache, and private exports to that project's `.gitignore` too.

## Credentials and command options

Supported key aliases are `ALPACA_API_KEY_ID`, `ALPACA_API_KEY`, and `APCA_API_KEY_ID`. Secret aliases are `ALPACA_API_SECRET_KEY`, `ALPACA_API_SECRET`, `ALPACA_SECRET_KEY`, and `APCA_API_SECRET_KEY`. Aliases are checked in that order within the selected source.

Credentials in the selected `.env` take precedence over shell variables. If the file has no Alpaca credential settings, the dashboard falls back to a complete pair from the process environment. A partial or empty credential pair in the file is an error; credentials are never combined across sources. File values are read without shell evaluation, interpolation, or modifications to the process environment.

Only `https://paper-api.alpaca.markets` is supported. Live-account and custom API origins are rejected. Credentials stay on the server and are never written to the cache or logged. The dashboard does not search parent directories or the installed package for credentials.

```bash
uv run alpaca-dashboard \
  --env-file .env \
  --cache-db data/dashboard.sqlite \
  --port 8050 \
  --refresh-seconds 5
```

| Option | Default | Behavior |
| --- | --- | --- |
| `--env-file PATH` | `.env` | Read credentials from this file, with the environment fallback described above. |
| `--cache-db PATH` | `data/dashboard.sqlite` | Use a local SQLite cache tied to one account. Parent directories are created as needed. |
| `--port INTEGER` | `8050` | Listen on `127.0.0.1`; valid ports are 1–65535. |
| `--refresh-seconds NUMBER` | `5` | Poll account data at this interval; must be finite and at least one second. |
| `--help` | — | Print command help and exit. |

All relative paths resolve against the launch directory. Run the command from the relevant project's root, or pass explicit paths. The module command `python -m alpaca_dashboard` accepts the same options.

## Account views

The account-value headline and dollar/percentage P&L follow the hovered chart point, returning to period totals when the pointer leaves. Chart and table ranges offer **1D, 1W, 1M, 3M, 6M, YTD, 1Y, ALL, and CUSTOM**. Controls start linked; a separate table selector appears when unlinked. Relinking applies the chart's range. Custom dates include both selected days in New York time.

1D follows the current or most recent regular NYSE session, including holidays, early closes, and DST. Longer ranges use progressively coarser history; ALL starts at account inception, subject to available broker records. Historical chart data refreshes once per minute and on range changes, while current equity updates between history refreshes. Chart zoom and table filters, selection, sorting, and pagination survive refreshes.

- **Trade history:** completed position lifecycles reconstructed from fills, including partial fills, scaling, shorts, fractional shares, and reversals. The selected range filters exit times. Select a trade for execution details.
- **Open now:** every current holding, using broker basis, quantities, prices, returns, and linked stop/target orders. Holdings remain visible regardless of the selected history range.
- **Orders:** broker orders and bracket relationships, filtered by submission time.

Tables support search, column filters, sorting, resizing, pagination, and CSV export. Data comes exclusively from the broker; no trading journal, strategy attribution, or project-specific metadata is used.

## Performance and storage

Account P&L is equity change minus external funding; the percentage uses starting equity. Fees, dividends, and financing remain part of account performance. Completed-trade returns are labeled **gross execution P&L**, calculated from actual fills, so table totals need not equal account performance. Monetary calculations use `Decimal` until presentation.

Missing history, unsupported instruments, security transfers, corporate actions, and unreconciled quantities are marked incomplete rather than assigned invented returns. Zero starting equity produces an unavailable percentage. Earlier zero-balance history is excluded when performance starts at the first recorded nonzero balance, with a dated informational note. Calculated trade returns target US equities; broker fields and executions remain visible for unsupported instruments.

Snapshots and history are cached in `data/dashboard.sqlite`, bound to the account ID. Use a separate cache path for another account. The cache contains account details, positions, orders, activities, and portfolio history; it is not encrypted. Existing dashboard caches remain compatible; obsolete strategy fields are ignored. All browser sessions share one polling worker per process; outages retain the last successful data and retry with bounded backoff.

History caches can grow as you explore additional ranges. To rebuild a cache, stop the dashboard and move the SQLite file to a private backup location before restarting. The next launch downloads available broker history again.

Credentials, databases, and trading state are not distributed with the package. Remote hosting, live trading accounts, journal replay, and streaming feeds are outside this version.

## Python API

Importing the package, constructing the client/service, and creating the app do not start polling. The service initializes its local cache when constructed. The caller owns lifecycle management:

```python
from pathlib import Path
from alpaca_dashboard import AlpacaReadOnlyClient, DashboardService, create_app

with AlpacaReadOnlyClient.from_env(env_file=Path(".env")) as client:
    service = DashboardService(client, cache_path=Path("data/dashboard.sqlite"))
    app = create_app(service)
    try:
        service.start()
        app.run(host="127.0.0.1", port=8050, debug=False, use_reloader=False)
    finally:
        service.close()
```

## Development

From this library checkout:

```bash
uv sync --frozen --all-groups
uv run --frozen --all-groups playwright install chromium
uv run --frozen --all-groups pytest
uv run --frozen --all-groups ruff check .
uv run --frozen --all-groups ruff format --check .
uv build
uv run --frozen --all-groups python .github/scripts/check_dist.py
```

Tests use synthetic accounts and fake broker transports; no real credentials or `.env` file are needed. Browser tests cover updates, linked ranges, hover values, UI persistence, empty accounts, outage recovery, CSV export, and narrow layouts. Missing Playwright or Chromium causes browser tests to skip, so install Chromium before checking the full suite. For a quick check without browser tests:

```bash
uv run --frozen --all-groups pytest --ignore=tests/test_dashboard_browser.py
```

The package includes its CSS and JavaScript assets and requires no frontend build step or paid grid features. `uv build` produces a source archive and wheel in `dist/`; the archive check verifies assets, licensing, and exclusion of local data. [GitHub CI](.github/workflows/ci.yml) runs tests on Python 3.12, 3.13, and 3.14, Chromium tests, lint and formatting checks, and package validation. See [CONTRIBUTING.md](CONTRIBUTING.md) for changes and bug reports.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Missing key or secret | Replace both values in the selected `.env`, or supply a complete environment pair. A partial or blank file pair blocks environment fallback. |
| Rejected API origin | Remove a live or custom base URL from the credential source; only the paper API origin is accepted. |
| HTTP 401 or 403 in the status banner | Check that both credentials belong to the intended paper account and are still valid. Restart after changing credentials. |
| Cache belongs to another account | Pass a different `--cache-db` path for each account. |
| Cache unavailable or startup failure | Confirm the cache directory is writable, disk space is available, and the selected port is free. Try another `--port` or cache path. |
| Stale data or initial history loading | Check the status banner and connection. The dashboard retries failed requests and retains the last available data; initial backfill may take time. |
| Unavailable or incomplete returns | Review the displayed data-status note. Missing records, unsupported instruments, corporate actions, and zero starting equity can prevent a reliable calculation. |
| Playwright cannot launch Chromium | Run the browser installation command above. On Linux, missing system libraries may require `uv run --frozen --all-groups playwright install --with-deps chromium`, which may need administrator access. |

When reporting a problem, use synthetic examples and sanitized error messages. Keep API keys, secrets, account IDs, caches, exports, and account screenshots out of public issues.

## License

Licensed under the [MIT License](LICENSE).
