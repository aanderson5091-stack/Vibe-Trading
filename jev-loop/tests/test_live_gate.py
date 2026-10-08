import pytest

from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import (
    LIVE_ALLOW_ENV_VALUE,
    LIVE_ALLOW_ENV_VAR,
    LIVE_CONFIRMATION_PHRASE,
    LIVE_TRADING_BASE_URL,
    PAPER_TRADING_BASE_URL,
    AlpacaConfigError,
    AlpacaPaperClient,
    LiveTradingRefused,
    client_from_env,
    resolve_trading_base_url,
)

BTC_SPEC = resolve_symbol("BTC/USD")


# -- resolve_trading_base_url(): the pure gate logic, no I/O -----------------


def test_paper_is_the_default_regardless_of_env_or_confirmation():
    # live=False must always win, even if the other two gates happen to be
    # set correctly -- paper stays the default everywhere unless --live is
    # explicitly passed.
    assert (
        resolve_trading_base_url(
            live=False,
            allow_live_env=LIVE_ALLOW_ENV_VALUE,
            confirmation=LIVE_CONFIRMATION_PHRASE,
        )
        == PAPER_TRADING_BASE_URL
    )
    assert (
        resolve_trading_base_url(live=False, allow_live_env=None, confirmation=None)
        == PAPER_TRADING_BASE_URL
    )


def test_live_refused_when_env_var_missing():
    with pytest.raises(LiveTradingRefused):
        resolve_trading_base_url(
            live=True, allow_live_env=None, confirmation=LIVE_CONFIRMATION_PHRASE
        )


def test_live_refused_when_env_var_wrong_value():
    with pytest.raises(LiveTradingRefused):
        resolve_trading_base_url(
            live=True, allow_live_env="yes", confirmation=LIVE_CONFIRMATION_PHRASE
        )


def test_live_refused_when_confirmation_missing():
    with pytest.raises(LiveTradingRefused):
        resolve_trading_base_url(
            live=True, allow_live_env=LIVE_ALLOW_ENV_VALUE, confirmation=None
        )


def test_live_refused_when_confirmation_does_not_match_exactly():
    with pytest.raises(LiveTradingRefused):
        resolve_trading_base_url(
            live=True,
            allow_live_env=LIVE_ALLOW_ENV_VALUE,
            confirmation="i understand this trades real money",  # wrong case/text
        )


def test_live_refused_when_flag_missing_even_with_the_other_two_set():
    # live=False short-circuits before either gate is even inspected.
    assert (
        resolve_trading_base_url(
            live=False,
            allow_live_env=LIVE_ALLOW_ENV_VALUE,
            confirmation=LIVE_CONFIRMATION_PHRASE,
        )
        == PAPER_TRADING_BASE_URL
    )


def test_live_routes_to_live_base_url_when_all_three_gates_pass():
    assert (
        resolve_trading_base_url(
            live=True,
            allow_live_env=LIVE_ALLOW_ENV_VALUE,
            confirmation=LIVE_CONFIRMATION_PHRASE,
        )
        == LIVE_TRADING_BASE_URL
    )


# -- AlpacaPaperClient: cannot reach the live URL except through the gate ---


def test_direct_construction_against_live_url_is_refused():
    with pytest.raises(AlpacaConfigError):
        AlpacaPaperClient(
            api_key="x", secret_key="y", spec=BTC_SPEC, base_url=LIVE_TRADING_BASE_URL
        )


def test_construction_against_live_url_succeeds_only_with_gate_flag():
    client = AlpacaPaperClient(
        api_key="x",
        secret_key="y",
        spec=BTC_SPEC,
        base_url=LIVE_TRADING_BASE_URL,
        _live_gate_passed=True,
    )
    assert client.base_url == LIVE_TRADING_BASE_URL
    assert client.is_live is True


# -- client_from_env(): the real call site every command in the CLI uses ---


def test_client_from_env_defaults_to_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.delenv(LIVE_ALLOW_ENV_VAR, raising=False)
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    client = client_from_env(symbol="BTC/USD")
    assert client.base_url == PAPER_TRADING_BASE_URL
    assert client.is_live is False


def test_client_from_env_live_flag_alone_is_refused(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.delenv(LIVE_ALLOW_ENV_VAR, raising=False)
    with pytest.raises(LiveTradingRefused):
        client_from_env(
            symbol="BTC/USD", live=True, confirmation=LIVE_CONFIRMATION_PHRASE
        )


def test_client_from_env_env_var_alone_is_refused(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.setenv(LIVE_ALLOW_ENV_VAR, LIVE_ALLOW_ENV_VALUE)
    with pytest.raises(LiveTradingRefused):
        client_from_env(symbol="BTC/USD", live=True, confirmation=None)


def test_client_from_env_confirmation_alone_is_refused(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.delenv(LIVE_ALLOW_ENV_VAR, raising=False)
    with pytest.raises(LiveTradingRefused):
        client_from_env(
            symbol="BTC/USD", live=True, confirmation=LIVE_CONFIRMATION_PHRASE
        )


def test_client_from_env_routes_to_live_when_all_three_present(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.setenv(LIVE_ALLOW_ENV_VAR, LIVE_ALLOW_ENV_VALUE)
    client = client_from_env(
        symbol="BTC/USD", live=True, confirmation=LIVE_CONFIRMATION_PHRASE
    )
    assert client.base_url == LIVE_TRADING_BASE_URL
    assert client.is_live is True


def test_client_from_env_ignores_live_env_var_when_live_flag_not_passed(monkeypatch):
    # Even with the env var correctly set, omitting --live (live=False, the
    # CLI default) must still produce a paper client. Paper is the default
    # everywhere unless a caller explicitly opts in with --live.
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.setenv(LIVE_ALLOW_ENV_VAR, LIVE_ALLOW_ENV_VALUE)
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    client = client_from_env(
        symbol="BTC/USD", live=False, confirmation=LIVE_CONFIRMATION_PHRASE
    )
    assert client.base_url == PAPER_TRADING_BASE_URL
    assert client.is_live is False
