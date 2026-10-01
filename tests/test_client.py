from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from http_helpers import Opener, Response, client, http_error

from alpaca_dashboard.client import (
    _ENV_NAMES,
    ALPACA_PAPER_BASE_URL,
    AlpacaAPIError,
    AlpacaReadOnlyClient,
    _NoRedirect,
)


@pytest.fixture
def empty_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://paper-api.alpaca.markets",
        "https://api.alpaca.markets",
        "https://paper-api.alpaca.markets.attacker.example",
        "https://paper-api.alpaca.markets@attacker.example",
        "https://paper-api.alpaca.markets:443",
        "https://paper-api.alpaca.markets/v2",
        "https://paper-api.alpaca.markets?redirect=evil",
        "https://paper-api.alpaca.markets#ignored",
        "https://paper-api.alpaca.markets\n",
    ],
)
def test_rejects_every_origin_except_paper_before_transport(base_url):
    with pytest.raises(ValueError, match="Only https://paper-api"):
        AlpacaReadOnlyClient("test-key", "test-secret", base_url=base_url)


def test_default_transport_forbids_redirects_and_client_context_closes():
    with AlpacaReadOnlyClient("test-key", "test-secret") as api:
        guards = [handler for handler in api._opener.handlers if isinstance(handler, _NoRedirect)]
        assert len(guards) == 1
        assert guards[0].redirect_request(None, None, 302, "", {}, "https://evil.test") is None
    api, opener = client(Response({"id": "account"}))
    with api:
        assert api.get_account() == {"id": "account"}
    assert opener.closed


@pytest.mark.parametrize(
    ("key", "secret"),
    [("", "secret"), ("key", ""), ("key\n", "secret"), ("key", "secret\rleak"), (" ", "s")],
)
def test_invalid_credentials_rejected_without_echoing_values(key, secret):
    with pytest.raises(ValueError, match="nonempty printable credentials"):
        AlpacaReadOnlyClient(key, secret)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_timeout_rejected(timeout):
    with pytest.raises(ValueError, match="timeout"):
        AlpacaReadOnlyClient("key", "secret", timeout=timeout)


@pytest.mark.usefixtures("empty_env")
@pytest.mark.parametrize(
    ("key_name", "secret_name"),
    [
        ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"),
        ("ALPACA_API_KEY", "ALPACA_API_SECRET"),
        ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
        ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
    ],
)
def test_dotenv_supports_aliases_quotes_comments_and_export(tmp_path, key_name, secret_name):
    env = tmp_path / ".env"
    env.write_text(
        f'export {key_name}="file-key" # comment\n{secret_name}=file-secret#suffix\n'
        "UNRELATED='unclosed\n",
        encoding="utf-8",
    )
    with AlpacaReadOnlyClient.from_env(env_file=env) as api:
        assert api._api_key == "file-key"
        assert api._api_secret == "file-secret#suffix"
        assert api.base_url == ALPACA_PAPER_BASE_URL


@pytest.mark.usefixtures("empty_env")
def test_environment_can_work_without_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    with AlpacaReadOnlyClient.from_env(env_file=tmp_path / "missing.env") as api:
        assert api._api_key == "env-key"


@pytest.mark.usefixtures("empty_env")
def test_missing_credentials_fail_without_request(tmp_path):
    with pytest.raises(ValueError, match="Missing Alpaca"):
        AlpacaReadOnlyClient.from_env(env_file=tmp_path / "missing.env")


@pytest.mark.usefixtures("empty_env")
def test_bad_quoted_secret_error_omits_secret(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_SECRET='do-not-show-this\n")
    with pytest.raises(ValueError, match="line 1") as caught:
        AlpacaReadOnlyClient.from_env(env_file=env)
    assert "do-not-show" not in str(caught.value)


@pytest.mark.usefixtures("empty_env")
def test_live_base_url_from_env_is_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "ALPACA_API_KEY=key\nALPACA_API_SECRET=secret\n"
        "APCA_API_BASE_URL=https://api.alpaca.markets\n"
    )
    with pytest.raises(ValueError, match="Only https://paper-api"):
        AlpacaReadOnlyClient.from_env(env_file=env)


def test_order_ids_cannot_modify_request_path_or_query():
    api, opener = client(Response({"id": "order"}))
    api.get_order("order/with?special#chars")
    assert "/v2/orders/order%2Fwith%3Fspecial%23chars?nested=true" in opener.calls[0][0].full_url


def test_open_orders_use_id_cursor_not_timestamps_and_retain_bracket_legs():
    orders = [
        {"id": "third", "submitted_at": "2026-09-28T14:00:00Z", "legs": [{"id": "stop"}]},
        {"id": "second", "submitted_at": "2026-09-28T14:00:00Z"},
        {"id": "first", "submitted_at": "2026-09-28T14:00:00Z"},
    ]
    api, opener = client(Response(orders[:2]), Response(orders[2:]))
    assert api.get_open_orders(page_size=2) == orders
    first = parse_qs(urlsplit(opener.calls[0][0].full_url).query)
    assert first == {"status": ["open"], "nested": ["true"], "direction": ["desc"], "limit": ["2"]}
    second = parse_qs(urlsplit(opener.calls[1][0].full_url).query)
    assert second == {**first, "before_order_id": ["second"]}


def test_full_final_page_requests_next_empty_page():
    api, opener = client(Response([{"id": "one"}]), Response([]))
    assert api.get_open_orders(page_size=1) == [{"id": "one"}]
    assert len(opener.calls) == 2


@pytest.mark.parametrize("rows", [[{"id": "one"}, {"id": "one"}], [{"symbol": "SPY"}]])
def test_open_orders_reject_duplicate_or_missing_ids(rows):
    api, _ = client(Response(rows))
    with pytest.raises(AlpacaAPIError, match="missing or repeated"):
        api.get_open_orders()


def test_open_orders_reject_ignored_cursor_instead_of_truncating():
    api, opener = client(Response([{"id": "one"}]), Response([{"id": "one"}]))
    with pytest.raises(AlpacaAPIError, match="repeated"):
        api.get_open_orders(page_size=1)
    assert len(opener.calls) == 2


@pytest.mark.parametrize(
    "response", [Response(body=b"not json test-secret"), Response([]), Response(None)]
)
def test_invalid_success_object_is_rejected_without_response_content(response):
    api, _ = client(response)
    with pytest.raises(AlpacaAPIError) as caught:
        api.get_account()
    assert "test-secret" not in str(caught.value)


def test_invalid_positions_array_rejected():
    api, _ = client(Response([None]))
    with pytest.raises(AlpacaAPIError, match="invalid list"):
        api.get_positions()


def test_paper_origin_cannot_be_changed_after_construction():
    api, _ = client()
    with pytest.raises(AttributeError):
        api.base_url = "https://api.alpaca.markets"


@pytest.mark.usefixtures("empty_env")
def test_file_pair_and_url_outrank_shell_as_one_source(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY_ID=file-key\nALPACA_API_SECRET_KEY=file-secret\n")
    monkeypatch.setenv("ALPACA_API_KEY", "shell-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "shell-secret")
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with AlpacaReadOnlyClient.from_env(env_file=env) as api:
        assert api._api_key == "file-key"
        assert api._api_secret == "file-secret"
        assert api.base_url == ALPACA_PAPER_BASE_URL


@pytest.mark.usefixtures("empty_env")
@pytest.mark.parametrize(
    "contents",
    [
        "ALPACA_API_KEY_ID=file-key\n",
        "ALPACA_API_SECRET_KEY=file-secret\n",
        "ALPACA_API_KEY=\nALPACA_API_SECRET=file-secret\n",
        "ALPACA_API_KEY=file-key\nALPACA_API_SECRET=\n",
    ],
)
def test_partial_file_pair_never_falls_back_to_shell(tmp_path, monkeypatch, contents):
    env = tmp_path / ".env"
    env.write_text(contents)
    monkeypatch.setenv("APCA_API_KEY_ID", "shell-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "shell-secret")
    with pytest.raises(ValueError, match="selected .env file") as caught:
        AlpacaReadOnlyClient.from_env(env_file=env)
    assert "file-key" not in str(caught.value)
    assert "file-secret" not in str(caught.value)


@pytest.mark.usefixtures("empty_env")
def test_file_without_credentials_uses_shell_pair_and_shell_url(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("UNRELATED=value\nAPCA_API_BASE_URL=https://api.alpaca.markets\n")
    monkeypatch.setenv("APCA_API_KEY_ID", "shell-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "shell-secret")
    with AlpacaReadOnlyClient.from_env(env_file=env) as api:
        assert api._api_key == "shell-key"
        assert api._api_secret == "shell-secret"
        assert api.base_url == ALPACA_PAPER_BASE_URL
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(ValueError, match="Only https://paper-api"):
        AlpacaReadOnlyClient.from_env(env_file=env)


@pytest.mark.usefixtures("empty_env")
@pytest.mark.parametrize("contents", ["ALPACA_API_KEY", "APCA_API_SECRET_KEY='unterminated"])
def test_malformed_known_file_settings_fail_before_shell_fallback(tmp_path, monkeypatch, contents):
    env = tmp_path / ".env"
    env.write_text(contents)
    monkeypatch.setenv("APCA_API_KEY_ID", "shell-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "shell-secret")
    with pytest.raises(ValueError, match="line 1"):
        AlpacaReadOnlyClient.from_env(env_file=env)


@pytest.mark.usefixtures("empty_env")
def test_each_destination_uses_its_own_working_directory_and_account(tmp_path, monkeypatch):
    accounts = []
    calls = []
    for name in ("first", "second"):
        project = tmp_path / name
        project.mkdir()
        (project / ".env").write_text(
            f"ALPACA_API_KEY={name}-key\nALPACA_API_SECRET={name}-secret\n"
        )
        opener = Opener(Response({"id": f"account-{name}"}))
        monkeypatch.setattr(
            "alpaca_dashboard.client.build_opener", lambda *_, opener=opener: opener
        )
        monkeypatch.chdir(project)
        with AlpacaReadOnlyClient.from_env() as api:
            accounts.append(api.get_account()["id"])
        request = opener.calls[0][0]
        calls.append(request)
        assert request.get_header("Apca-api-key-id") == f"{name}-key"
        assert request.get_header("Apca-api-secret-key") == f"{name}-secret"
    assert accounts == ["account-first", "account-second"]
    assert all(request.get_method() == "GET" for request in calls)


@pytest.mark.usefixtures("empty_env")
def test_default_env_lookup_does_not_search_parent_projects(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ALPACA_API_KEY=parent\nALPACA_API_SECRET=parent-secret\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ValueError, match="Missing Alpaca"):
        AlpacaReadOnlyClient.from_env()


def test_public_client_has_only_read_endpoints_and_get_transport():
    api, opener = client(
        Response({"id": "account"}),
        Response({"is_open": True}),
        Response([{"symbol": "SPY"}]),
        Response({"id": "order", "legs": [{"id": "stop"}]}),
    )
    assert api.get_account()["id"] == "account"
    assert api.get_clock()["is_open"]
    assert api.get_positions()[0]["symbol"] == "SPY"
    assert api.get_order("order")["legs"] == [{"id": "stop"}]
    assert not any(hasattr(api, name) for name in ("submit_order", "cancel_order", "reconcile"))
    for request, timeout in opener.calls:
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.get_header("Apca-api-key-id") == "test-key"
        assert request.get_header("Apca-api-secret-key") == "test-secret"
        assert "test-secret" not in request.full_url
        assert timeout == 30


@pytest.mark.parametrize("status", [302, 401, 422, 500])
def test_http_errors_are_sanitized_without_automatic_retries(status):
    api, opener = client(http_error(status))
    with pytest.raises(AlpacaAPIError) as caught:
        api.get_account()
    assert caught.value.status == status
    assert "test-key" not in str(caught.value)
    assert "test-secret" not in str(caught.value)
    assert len(opener.calls) == 1


@pytest.mark.parametrize("failure", [URLError("test-secret"), TimeoutError("test-key")])
def test_network_errors_are_sanitized_without_automatic_retries(failure):
    api, opener = client(failure)
    with pytest.raises(AlpacaAPIError, match="transport") as caught:
        api.get_account()
    assert caught.value.status is None
    assert "test-secret" not in str(caught.value)
    assert "test-key" not in str(caught.value)
    assert len(opener.calls) == 1
