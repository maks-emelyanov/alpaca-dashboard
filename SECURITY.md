# Security policy

## Intended use

Alpaca Dashboard is a local application for a trusted machine and an Alpaca paper account. The CLI binds to `127.0.0.1`, disables debug mode and the reloader, and provides no authentication or authorization for the web UI. Anyone who can access that server can see its account data. Network hosting, port forwarding, shared-machine isolation, and live accounts are outside the supported scope.

The broker client makes GET requests only to `https://paper-api.alpaca.markets` and refuses redirects. This restricts the dashboard's behavior; it does not reduce the permissions of the API key itself. Credentials remain in the server process and are not intentionally logged, cached, or sent to the browser. Broker and transport errors are sanitized before display.

The local `.env`, SQLite cache, browser-visible data, and CSV exports are sensitive. The cache contains account information and trading records without application-level encryption. Access is controlled by the operating system and the permissions of the files and their directories. Git ignore rules help avoid accidental commits but do not protect files already tracked by Git or files shared separately.

## Reporting a vulnerability

This repository is private. Authorized collaborators can open an issue to report a vulnerability, using synthetic data and sanitized examples. Include the affected commit or version, reproduction steps, and impact. If details require a more restricted audience, open a minimal issue requesting a private reporting channel first.

If the repository becomes public, use **Report a vulnerability** on its GitHub **Security** tab when private vulnerability reporting is enabled. Otherwise request a private reporting channel without publishing exploit details. Keep credentials and account information out of all reports. Ordinary functional bugs can be reported using [CONTRIBUTING.md](CONTRIBUTING.md).

When possible, check whether the issue also affects the current default branch. There is no separate security maintenance policy for older releases.

## Exposed credentials

If a key or secret is exposed, revoke or rotate it in the Alpaca account where it was created. Update the selected credential source and restart the dashboard. Removing a value from a new commit does not remove it from existing Git history, public copies, or other people's clones.
