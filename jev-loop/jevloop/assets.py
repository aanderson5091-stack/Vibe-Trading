"""Asset resolver: turns any symbol into a spec.

Two asset classes: crypto (24/7, e.g. BTC/USD, ETH/USD) and us_equity
(market hours only, e.g. AAPL, SPY, TSLA, NVDA). Everything asset-specific,
which Alpaca endpoints to call, how big an order must be, how many
decimals a quantity can have, whether shorting is allowed, whether the
venue is open right now, lives here. The rest of the loop reads an
AssetSpec and never special-cases a symbol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CRYPTO_DATA_BASE = "https://data.alpaca.markets/v1beta3/crypto/us"
EQUITY_DATA_BASE = "https://data.alpaca.markets/v2/stocks"

_CRYPTO_PATTERN = re.compile(r"^[A-Z0-9]{2,10}/(USD|USDT|USDC)$")
_EQUITY_PATTERN = re.compile(r"^[A-Z]{1,5}$")

# Tick sizes for the symbols the video and the article actually name.
# Anything else falls back to the class default below.
_KNOWN_TICK_SIZE = {
    "BTC/USD": 0.01,
    "ETH/USD": 0.01,
}


class UnknownSymbolError(Exception):
    pass


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    asset_class: str  # "crypto" | "us_equity"
    tick_size: float
    min_notional_usd: float
    qty_precision: int
    shorting_allowed: bool
    is_24_7: bool
    has_depth: bool  # False on venues where only best bid/ask is available
    orderbook_url: str | None
    latest_trade_url: str
    recent_trades_url: str
    latest_quote_url: str


def resolve_symbol(raw: str) -> AssetSpec:
    """Resolve any symbol Lewis (or the viewer) might type. Raises
    UnknownSymbolError with a plain-English message on anything that
    matches neither a crypto pair nor a US equity ticker."""
    sym = raw.strip().upper()

    if _CRYPTO_PATTERN.match(sym):
        return AssetSpec(
            symbol=sym,
            asset_class="crypto",
            tick_size=_KNOWN_TICK_SIZE.get(sym, 0.01),
            min_notional_usd=10.0,  # Alpaca's crypto minimum order notional
            qty_precision=8,
            shorting_allowed=False,  # spot only, no naked shorting
            is_24_7=True,
            has_depth=True,  # crypto v1beta3 gives real L2 depth
            orderbook_url=f"{CRYPTO_DATA_BASE}/latest/orderbooks",
            latest_trade_url=f"{CRYPTO_DATA_BASE}/latest/trades",
            recent_trades_url=f"{CRYPTO_DATA_BASE}/trades",
            latest_quote_url=f"{CRYPTO_DATA_BASE}/latest/quotes",
        )

    if _EQUITY_PATTERN.match(sym):
        return AssetSpec(
            symbol=sym,
            asset_class="us_equity",
            tick_size=0.01,
            min_notional_usd=1.0,  # Alpaca supports fractional notional equity orders
            qty_precision=4,
            shorting_allowed=False,  # this account is cash, not marginable
            is_24_7=False,
            has_depth=False,  # the basic equities feed has no L2 book
            orderbook_url=None,
            latest_trade_url=f"{EQUITY_DATA_BASE}/trades/latest",
            recent_trades_url=f"{EQUITY_DATA_BASE}/trades",
            latest_quote_url=f"{EQUITY_DATA_BASE}/quotes/latest",
        )

    raise UnknownSymbolError(
        f"'{raw}' does not look like a crypto pair (BTC/USD, ETH/USD, ...) or a "
        "US equity ticker (AAPL, SPY, TSLA, NVDA, ...). Pick one of those forms."
    )


def size_order(notional_usd: float, price: float, spec: AssetSpec) -> float:
    """Convert a target notional (dollars) into a quantity, rounded to the
    asset's precision, bumped up if rounding pushed it back under the
    venue's minimum notional.

    Sizing by notional rather than a fixed quantity is the fix for a real
    bug: a fixed 0.0002 BTC order is comfortably above Alpaca's $10 crypto
    minimum most of the time, but the same fixed quantity on a $30 stock,
    or during a crypto drawdown, can fall under the venue floor and get
    rejected. Sizing from a dollar target is honest across any asset and
    any price.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    target = max(notional_usd, spec.min_notional_usd)
    qty = target / price
    quantized = round(qty, spec.qty_precision)
    if quantized * price < spec.min_notional_usd:
        step = 10 ** (-spec.qty_precision)
        guard = 0
        while quantized * price < spec.min_notional_usd and guard < 10_000:
            quantized = round(quantized + step, spec.qty_precision)
            guard += 1
    return quantized
