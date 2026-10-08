import pytest

from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import (
    PAPER_TRADING_BASE_URL,
    AlpacaConfigError,
    AlpacaPaperClient,
    MarketClosedError,
    assert_paper_url,
)

BTC_SPEC = resolve_symbol("BTC/USD")


def test_paper_url_passes():
    assert_paper_url(PAPER_TRADING_BASE_URL)
    assert_paper_url(PAPER_TRADING_BASE_URL + "/")  # trailing slash tolerated


def test_live_url_is_refused():
    with pytest.raises(AlpacaConfigError):
        assert_paper_url("https://api.alpaca.markets")


def test_client_construction_refuses_live_base_url():
    with pytest.raises(AlpacaConfigError):
        AlpacaPaperClient(
            api_key="x",
            secret_key="y",
            spec=BTC_SPEC,
            base_url="https://api.alpaca.markets",
        )


def test_client_construction_accepts_paper_base_url():
    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=BTC_SPEC)
    assert client.base_url == PAPER_TRADING_BASE_URL


def test_client_construction_works_for_equity_spec():
    equity_spec = resolve_symbol("AAPL")
    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=equity_spec)
    assert client.symbol == "AAPL"
    assert client.spec.asset_class == "us_equity"


# -- closed-market guard: never place an equity order while the market is shut --


def test_limit_order_refused_when_equity_market_closed():
    equity_spec = resolve_symbol("AAPL")
    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=equity_spec)
    client.is_market_open = lambda: False  # no network: force the closed branch
    with pytest.raises(MarketClosedError):
        client.submit_limit_order("buy", 1.0, 100.0)


def test_market_order_refused_when_equity_market_closed():
    equity_spec = resolve_symbol("AAPL")
    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=equity_spec)
    client.is_market_open = lambda: False
    with pytest.raises(MarketClosedError):
        client.submit_market_order("sell", 1.0)


def test_crypto_never_checks_market_hours():
    # is_24_7 short-circuits the check entirely; is_market_open should not
    # even be consulted for a crypto spec. No network: _request is stubbed.
    crypto_client = AlpacaPaperClient(api_key="x", secret_key="y", spec=BTC_SPEC)

    def _boom():
        raise AssertionError("is_market_open should never be called for a 24/7 asset")

    crypto_client.is_market_open = _boom
    crypto_client._request = lambda method, url, **kwargs: {"id": "fake-order"}

    result = crypto_client.submit_limit_order("buy", 0.001, 100.0)
    assert result == {"id": "fake-order"}
