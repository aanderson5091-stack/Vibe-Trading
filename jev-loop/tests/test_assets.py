import pytest

from jevloop.assets import UnknownSymbolError, resolve_symbol, size_order


def test_resolves_known_crypto_pairs():
    for sym in ("BTC/USD", "ETH/USD", "SOL/USD"):
        spec = resolve_symbol(sym)
        assert spec.asset_class == "crypto"
        assert spec.is_24_7 is True
        assert spec.has_depth is True
        assert spec.min_notional_usd == 10.0


def test_resolves_known_equity_tickers():
    for sym in ("AAPL", "SPY", "TSLA", "NVDA"):
        spec = resolve_symbol(sym)
        assert spec.asset_class == "us_equity"
        assert spec.is_24_7 is False
        assert spec.has_depth is False


def test_resolution_is_case_insensitive():
    assert resolve_symbol("btc/usd").symbol == "BTC/USD"
    assert resolve_symbol("aapl").symbol == "AAPL"


def test_unknown_symbol_raises_with_a_helpful_message():
    with pytest.raises(UnknownSymbolError) as exc_info:
        resolve_symbol("not a real symbol!!")
    assert "BTC/USD" in str(exc_info.value)
    assert "AAPL" in str(exc_info.value)


def test_equity_has_no_orderbook_url():
    spec = resolve_symbol("AAPL")
    assert spec.orderbook_url is None


def test_crypto_has_an_orderbook_url():
    spec = resolve_symbol("BTC/USD")
    assert spec.orderbook_url is not None


# -- size_order: never below the venue minimum --------------------------


def test_size_order_meets_crypto_minimum_at_high_price():
    spec = resolve_symbol("BTC/USD")
    qty = size_order(notional_usd=20.0, price=85_000.0, spec=spec)
    assert qty * 85_000.0 >= spec.min_notional_usd


def test_size_order_meets_crypto_minimum_at_low_target():
    # A tiny requested notional must still clear the venue floor -- this is
    # exactly the bug a fixed quantity had: 0.0002 BTC cleared $10 most of
    # the time, but nothing enforced it when price moved.
    spec = resolve_symbol("BTC/USD")
    qty = size_order(notional_usd=0.01, price=85_000.0, spec=spec)
    assert qty * 85_000.0 >= spec.min_notional_usd


def test_size_order_meets_equity_minimum():
    spec = resolve_symbol("AAPL")
    qty = size_order(notional_usd=0.10, price=337.0, spec=spec)
    assert qty * 337.0 >= spec.min_notional_usd


def test_size_order_respects_precision():
    spec = resolve_symbol("AAPL")
    qty = size_order(notional_usd=50.0, price=337.03, spec=spec)
    # qty_precision is 4 for equities
    assert round(qty, spec.qty_precision) == qty


def test_size_order_rejects_non_positive_price():
    spec = resolve_symbol("BTC/USD")
    with pytest.raises(ValueError):
        size_order(notional_usd=20.0, price=0.0, spec=spec)
