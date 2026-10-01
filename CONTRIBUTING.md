# Contributing

For setup and supported behavior, start with [README.md](README.md). Fork the repository on GitHub, clone your fork, and work from a branch using Python 3.12 or newer and `uv`. Open pull requests against this repository's `main` branch.

## Development checks

```bash
uv sync --frozen --all-groups
uv run --frozen --all-groups playwright install chromium
uv run --frozen --all-groups ruff check .
uv run --frozen --all-groups ruff format --check .
uv run --frozen --all-groups pytest
uv build
uv run --frozen --all-groups python .github/scripts/check_dist.py
```

Tests use synthetic accounts and fake broker transports. They require no Alpaca credentials, `.env`, or live broker connection. Playwright tests use a local server and headless Chromium; install the browser before treating a full test run as complete. The README includes Linux browser troubleshooting and a command to run without browser tests.

Use the committed `uv.lock` for development. When intentionally changing dependencies, update `pyproject.toml`, run `uv lock`, and include the lockfile change. Packaging must continue to include the CSS and JavaScript assets and LICENSE without requiring a frontend build. The archive check expects one wheel and one source archive in `dist/`; move older build artifacts elsewhere when checking a new version.

## Pull requests

Describe the problem, resulting behavior, and checks you ran. Add focused regression coverage for behavior changes and update documentation when commands or user-visible behavior change. Keep unrelated changes separate.

Preserve the GET-only paper-account client, loopback CLI binding, sanitized errors, explicit service lifecycle, account-bound cache, and conservative handling of incomplete financial data. Use synthetic fixtures for accounting and API changes; do not introduce real account records into tests.

## Bug reports and questions

Use a GitHub issue for reproducible bugs or feature requests. Include your Python version, operating system, package version or commit, steps to reproduce, expected behavior, actual behavior, and a sanitized error message. For range or accounting issues, give the selected dates/timeframe and a minimal synthetic example.

Never include API keys, secrets, environment dumps, account IDs, private cache files, CSV exports, or screenshots containing account data. Report potential vulnerabilities through the process in [SECURITY.md](SECURITY.md).

Contributions are distributed under the repository's [MIT License](LICENSE).
