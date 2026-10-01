"""The standalone command owns only its reader and local web application."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from alpaca_dashboard.cli import dashboard_command, main, parser


def test_dashboard_parser_defaults_and_overrides():
    args = parser().parse_args([])
    assert args.port == 8050
    assert args.refresh_seconds == 5
    assert args.env_file == Path(".env")
    assert args.cache_db == Path("data/dashboard.sqlite")
    assert not hasattr(args, "db")
    custom = parser().parse_args(
        [
            "--port",
            "8060",
            "--refresh-seconds",
            "10",
            "--env-file",
            "paper.env",
            "--cache-db",
            "other.sqlite",
        ]
    )
    assert custom.port == 8060
    assert custom.refresh_seconds == 10
    assert custom.env_file == Path("paper.env")
    assert custom.cache_db == Path("other.sqlite")


@pytest.mark.parametrize(
    ("port", "interval", "message"),
    [
        (0, 5, "--port"),
        (65536, 5, "--port"),
        (8050, 0, "--refresh"),
        (8050, float("nan"), "--refresh"),
        (8050, float("inf"), "--refresh"),
    ],
)
def test_invalid_options_fail_before_credentials_or_network(port, interval, message):
    with pytest.raises(ValueError, match=message):
        dashboard_command(SimpleNamespace(port=port, refresh_seconds=interval))


@pytest.mark.parametrize("fail", [False, True])
def test_command_starts_explicitly_binds_locally_and_closes_owned_resources(monkeypatch, fail):
    events = []

    class Client:
        @classmethod
        def from_env(cls, **kwargs):
            events.append(("credentials", kwargs))
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            events.append("client-close")

    class Service:
        def __init__(self, client, **kwargs):
            assert isinstance(client, Client)
            events.append(("service", kwargs))

        def start(self):
            events.append("start")

        def close(self):
            events.append("service-close")

    def run(**kwargs):
        events.append(("run", kwargs))
        if fail:
            raise RuntimeError("bind failed")

    def create_app(service):
        assert isinstance(service, Service)
        assert "start" not in events
        events.append("app")
        return SimpleNamespace(run=run)

    monkeypatch.setattr("alpaca_dashboard.client.AlpacaReadOnlyClient", Client)
    monkeypatch.setattr("alpaca_dashboard.service.DashboardService", Service)
    monkeypatch.setattr("alpaca_dashboard.ui.create_app", create_app)
    args = parser().parse_args(["--port", "8060", "--env-file", "custom.env"])
    if fail:
        with pytest.raises(RuntimeError, match="bind failed"):
            dashboard_command(args)
    else:
        dashboard_command(args)
    assert events == [
        ("credentials", {"env_file": Path("custom.env"), "timeout": 10}),
        ("service", {"cache_path": Path("data/dashboard.sqlite"), "refresh_seconds": 5}),
        "app",
        "start",
        ("run", {"host": "127.0.0.1", "port": 8060, "debug": False, "use_reloader": False}),
        "service-close",
        "client-close",
    ]


def test_main_reports_configuration_errors_and_sanitizes_unexpected_failures(monkeypatch, capsys):
    assert main(["--port", "0"]) == 1
    assert "--port" in capsys.readouterr().err

    def fail(_):
        raise RuntimeError("transport leaked secret-key")

    monkeypatch.setattr("alpaca_dashboard.cli.dashboard_command", fail)
    assert main([]) == 1
    stderr = capsys.readouterr().err
    assert "could not start" in stderr
    assert "secret-key" not in stderr
