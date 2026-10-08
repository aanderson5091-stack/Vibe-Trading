"""The deterministic state snapshot.

Everything computable stays in code. This module never calls Jev. It turns
an order book (or, on venues with no L2 depth, best bid/ask), a slice of
recent trades, and the loop's own bookkeeping (inventory, PnL, health,
session VWAP) into one compact dict under roughly 400 tokens.

Timestamp discipline: every price, trade and fill carries the real
timestamp the venue stamped on it (or, for the loop's own mid samples, the
wall-clock time it was read). Every field is computed only from points at
or before `as_of`, so nothing that happened after the decision clock
started can leak into the snapshot.

Honest degradation: on an asset with no Level 2 depth (US equities on the
basic feed), the caller passes empty depth lists rather than fabricated
ones. `imbalance` and the depth fields come back `None` / empty in that
case instead of a fake "balanced" reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _pct_return(
    prices: list[tuple[float, float]], now: float, lookback_s: float
) -> float | None:
    """Return over the last lookback_s seconds using only points before `now`."""
    past = [p for ts, p in prices if ts <= now - lookback_s]
    current = [p for ts, p in prices if ts <= now]
    if not past or not current:
        return None
    base = past[-1]
    latest = current[-1]
    if base == 0:
        return None
    return (latest - base) / base


def _realised_vol(
    prices: list[tuple[float, float]],
    now: float,
    window_s: float,
    step_s: float = 60.0,
) -> float | None:
    """Realised volatility: stdev of log returns on a fixed `step_s` grid
    inside the window, using only points at or before `now`. The loop's
    history mixes one-minute bars (the startup backfill) with a real mid
    sample every tick, so the series is resampled to one grid before any
    return is taken: the last real price at or before each grid time.
    Returns None rather than a number when there is not enough real
    history yet."""
    import math

    pts = sorted((ts, p) for ts, p in prices if ts <= now and p > 0)
    if not pts:
        return None
    n_steps = int(window_s // step_s)
    grid = [now - k * step_s for k in range(n_steps, -1, -1)]
    sampled = []
    j = 0
    last = None
    for g in grid:
        while j < len(pts) and pts[j][0] <= g:
            last = pts[j][1]
            j += 1
        if last is not None:
            sampled.append(last)
    if len(sampled) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(sampled, sampled[1:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0))


@dataclass
class InventoryState:
    """The loop's own bookkeeping, carried tick to tick. Not fetched from
    Alpaca. Session VWAP and slippage are both computed here, in code:
    Jev never sees a raw trade tape, only the numbers this file derives
    from it."""

    inventory: float = 0.0
    entry_price: float = 0.0
    position_opened_at: float | None = None
    realised_pnl_usd: float = 0.0
    high_water_mark_usd: float = 0.0
    equity_usd: float = 0.0  # starting equity; seeded from the account at startup
    fills: int = 0
    fees_usd: float = 0.0  # fees Alpaca took (crypto buys pay theirs in coin)
    orders_submitted: int = 0
    orders_rejected: int = 0
    api_error_streak: int = 0
    recent_latencies_ms: list[float] = field(default_factory=list)
    recent_slippage_bps: list[float] = field(default_factory=list)
    # Session VWAP accumulators. Reset when the loop process restarts;
    # that is an honest limitation (documented in README.md), not a bug.
    vwap_cum_pv: float = 0.0
    vwap_cum_vol: float = 0.0
    vwap_last_trade_ts: float = 0.0


def update_vwap(inv: InventoryState, trades: list[tuple[float, float, float]]) -> None:
    """Fold newly-seen trades (timestamp, price, size) into the running
    session VWAP accumulators. Only trades strictly newer than the last
    one already folded in are counted, so calling this every tick with an
    overlapping recent-trades window never double-counts a fill."""
    newest_ts = inv.vwap_last_trade_ts
    for ts, price, size in trades:
        if ts <= inv.vwap_last_trade_ts:
            continue
        if price <= 0 or size <= 0:
            continue
        inv.vwap_cum_pv += price * size
        inv.vwap_cum_vol += size
        newest_ts = max(newest_ts, ts)
    inv.vwap_last_trade_ts = newest_ts


def record_fill_slippage(
    inv: InventoryState, expected_price: float, fill_price: float, side: str
) -> None:
    """Signed slippage in basis points: positive means the fill was worse
    than expected (paid more on a buy, received less on a sell)."""
    if expected_price <= 0:
        return
    sign = 1.0 if side == "buy" else -1.0
    bps = sign * (fill_price - expected_price) / expected_price * 10_000
    inv.recent_slippage_bps.append(round(bps, 2))
    inv.recent_slippage_bps = inv.recent_slippage_bps[-10:]


def apply_fill(
    inv: InventoryState, side: str, qty: float, price: float, ts: float
) -> None:
    """Fold one confirmed fill (from the broker, never an assumed one) into
    inventory, average entry price and realised PnL. Average-cost
    accounting: reducing a position realises (price - entry) on the
    reduced quantity; crossing through zero opens the remainder at the
    fill price."""
    if qty <= 0 or price <= 0:
        return
    signed = qty if side == "buy" else -qty
    old = inv.inventory
    new = old + signed
    if old == 0 or (old > 0) == (signed > 0):
        # opening or adding: new weighted-average entry
        inv.entry_price = (
            (inv.entry_price * abs(old) + price * qty) / abs(new) if new else 0.0
        )
        if old == 0:
            inv.position_opened_at = ts
    else:
        closed = min(abs(old), qty)
        direction = 1.0 if old > 0 else -1.0
        inv.realised_pnl_usd += (price - inv.entry_price) * closed * direction
        if abs(new) < 1e-12:
            new = 0.0
            inv.entry_price = 0.0
            inv.position_opened_at = None
        elif (new > 0) != (old > 0):
            inv.entry_price = price
            inv.position_opened_at = ts
    inv.inventory = new
    inv.fills += 1


def build_snapshot(
    *,
    as_of: float,
    mid: float,
    microprice: float,
    spread_bps: float,
    bid_depth: list[tuple[float, float]],
    ask_depth: list[tuple[float, float]],
    trade_prices: list[tuple[float, float]],
    trade_sides: list[tuple[float, str]],
    inv: InventoryState,
    data_timestamp: float,
    has_depth: bool = True,
) -> dict:
    """Assemble the deterministic snapshot. All list inputs must already be
    filtered to timestamps <= as_of by the caller (execution/alpaca.py).

    `has_depth` is False for venues with no Level 2 book (equities on the
    basic feed): `bid_depth`/`ask_depth` are then expected to hold at most
    one (price, size) pair each, taken from the best bid/ask of the latest
    quote, and `imbalance` is computed from that single level rather than
    three, or left `None` if even that is unavailable."""

    bid_sz = sum(sz for _, sz in bid_depth[:3])
    ask_sz = sum(sz for _, sz in ask_depth[:3])
    if bid_sz + ask_sz > 0:
        imbalance = round((bid_sz - ask_sz) / (bid_sz + ask_sz), 4)
    else:
        imbalance = None

    recent_trades = [(ts, side) for ts, side in trade_sides if ts <= as_of]
    window_trades = [s for ts, s in recent_trades if ts >= as_of - 30.0]
    buys = sum(1 for s in window_trades if s == "buy")
    aggressive_buy_ratio = buys / len(window_trades) if window_trades else 0.5
    trade_intensity = len(window_trades) / 30.0  # trades per second, trailing 30s

    unrealised_pnl = (mid - inv.entry_price) * inv.inventory if inv.inventory else 0.0
    # equity_usd is the starting equity the loop was seeded with (the real
    # account equity at startup), so drawdown is measured against real money,
    # never against zero. The loop raises high_water_mark_usd tick by tick.
    equity = inv.equity_usd + inv.realised_pnl_usd + unrealised_pnl
    peak = max(inv.high_water_mark_usd, equity)
    drawdown_pct = (peak - equity) / peak if peak > 0 else 0.0
    position_age_s = (
        (as_of - inv.position_opened_at)
        if (inv.inventory and inv.position_opened_at)
        else 0.0
    )

    fill_ratio = (
        min(1.0, inv.fills / inv.orders_submitted) if inv.orders_submitted else 1.0
    )
    data_age_s = max(0.0, as_of - data_timestamp)

    vwap = inv.vwap_cum_pv / inv.vwap_cum_vol if inv.vwap_cum_vol > 0 else mid

    return {
        "as_of": as_of,
        # PRICE
        "mid": mid,
        "microprice": microprice,
        "vwap": round(vwap, 6),
        "return_1m": _pct_return(trade_prices, as_of, 60),
        "return_5m": _pct_return(trade_prices, as_of, 300),
        "return_30m": _pct_return(trade_prices, as_of, 1800),
        # BOOK (depth fields are honestly empty/None where the venue has no L2 book)
        "spread_bps": spread_bps,
        "has_depth": has_depth,
        "depth_levels_available": (
            min(len(bid_depth), len(ask_depth)) if has_depth else 0
        ),
        "bid_depth_3": [[p, s] for p, s in bid_depth[:3]],
        "ask_depth_3": [[p, s] for p, s in ask_depth[:3]],
        "imbalance": imbalance,
        # FLOW
        "aggressive_buy_ratio": round(aggressive_buy_ratio, 4),
        "trade_intensity_per_s": round(trade_intensity, 4),
        # VOL
        "realised_vol_short": _realised_vol(trade_prices, as_of, 300),
        "realised_vol_medium": _realised_vol(trade_prices, as_of, 1800),
        # BOOK PnL
        "inventory": inv.inventory,
        "unrealised_pnl_usd": round(unrealised_pnl, 4),
        "daily_loss_usd": round(max(0.0, -inv.realised_pnl_usd - unrealised_pnl), 4),
        "drawdown_pct": round(drawdown_pct, 6),
        "position_age_s": round(position_age_s, 1),
        # HEALTH
        "fill_ratio": round(fill_ratio, 4),
        "reject_count": inv.orders_rejected,
        "last_10_latencies_ms": inv.recent_latencies_ms[-10:],
        "last_10_slippage_bps": inv.recent_slippage_bps[-10:],
        "data_age_s": round(data_age_s, 3),
        "leverage": 1.0,
    }


def approx_token_count(snapshot: dict) -> int:
    """Rough token estimate (chars / 4) so the loop can print a sanity check
    that the snapshot stays well under the 400-token guideline."""
    import json

    return len(json.dumps(snapshot, default=str)) // 4
