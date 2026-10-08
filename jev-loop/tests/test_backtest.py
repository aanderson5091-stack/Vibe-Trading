"""Tests for the bar-replay backtester. All synthetic bars, no network."""

from jevloop.backtest import (
    BacktestConfig,
    simulate,
    simulate_maker,
    format_report,
    format_maker_report,
    _normalise_bars,
)


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def flat_then_rally(n_flat=5, n_up=10, start=80_000.0, step=80.0):
    """A flat stretch (no momentum) then a steady rally (~10 bps/bar, above
    the ~5.25 bps momentum-entry threshold) that should trip the entry and
    ride up."""
    bars = [bar(start, start, start, start) for _ in range(n_flat)]
    p = start
    for _ in range(n_up):
        o = p
        p = p + step
        bars.append(bar(o, p, o, p))
    return bars


def test_no_trades_on_flat_series():
    bars = [bar(80_000, 80_000, 80_000, 80_000) for _ in range(30)]
    res = simulate(bars, BacktestConfig())
    assert res.n_trades == 0
    assert res.net_pnl_usd == 0.0


def test_too_few_bars_returns_empty():
    res = simulate([bar(1, 1, 1, 1), bar(1, 1, 1, 1)], BacktestConfig())
    assert res.n_trades == 0
    assert res.n_bars == 2


def test_rally_triggers_a_long_and_books_a_trade():
    bars = flat_then_rally()
    # zero costs so the mechanics are easy to assert
    res = simulate(bars, BacktestConfig(fee_bps_per_side=0.0, spread_bps=0.0))
    assert res.n_trades >= 1
    t = res.trades[0]
    # entry fill precedes exit fill in bar index (no same-bar round trip)
    assert t.exit_i > t.entry_i
    # with zero costs on a rising series the first scalp is green
    assert t.gross_bps > 0
    assert abs(t.net_bps - t.gross_bps) < 1e-6  # no costs -> net == gross


def test_costs_reduce_net_below_gross():
    bars = flat_then_rally()
    res = simulate(bars, BacktestConfig(fee_bps_per_side=15.0, spread_bps=2.0))
    assert res.n_trades >= 1
    # every trade's net is below its gross by exactly the round-trip cost
    rt = 2 * res.cfg.cost_bps_per_side
    for t in res.trades:
        assert t.gross_bps - t.net_bps > 0
        # net is gross minus ~round-trip cost (within rounding on price ratios)
        assert abs((t.gross_bps - t.net_bps) - rt) < 1.0


def test_fees_dwarf_small_take_profit():
    """The instructive result: even on a rising series the scalp loses money
    net, because the ~32 bps round-trip taker cost dwarfs the 8 bps
    take-profit the exit aims for."""
    bars = flat_then_rally()
    res = simulate(bars, BacktestConfig(fee_bps_per_side=15.0, spread_bps=2.0))
    assert res.n_trades >= 1
    assert res.gross_bps_total > 0  # price rose -> gross is positive
    assert res.net_pnl_usd < 0  # ... but costs turn it net negative


def test_equity_curve_and_drawdown_consistent():
    bars = flat_then_rally()
    res = simulate(bars, BacktestConfig(fee_bps_per_side=15.0, spread_bps=2.0))
    # equity curve has one point per closed trade, cumulative
    assert len(res.equity_usd) == res.n_trades
    assert res.max_drawdown_usd <= 0.0


def test_open_position_is_force_closed_at_end():
    # Enter on the very last up-bars so there is no time to hit TP/age before
    # the data ends: the open long must be marked out, not left dangling.
    bars = flat_then_rally(n_flat=10, n_up=2)
    res = simulate(bars, BacktestConfig(fee_bps_per_side=0.0, spread_bps=0.0))
    assert res.n_trades == 1
    assert "forced close" in res.trades[0].reason


def test_normalise_bars_skips_malformed():
    raw = [{"o": "1", "h": "2", "l": "0.5", "c": "1.5"}, {"o": "x"}, {}]
    out = _normalise_bars(raw)
    assert len(out) == 1
    assert out[0]["c"] == 1.5


def test_report_renders_without_trades():
    bars = [bar(80_000, 80_000, 80_000, 80_000) for _ in range(5)]
    res = simulate(bars, BacktestConfig())
    txt = format_report(res, "BTC/USD")
    assert "no trades" in txt
    assert "HONEST SCOPE" in txt


def test_report_renders_with_trades():
    res = simulate(flat_then_rally(), BacktestConfig())
    txt = format_report(res, "BTC/USD")
    assert "win rate" in txt
    assert "net P&L" in txt


# ========================= maker / spread-capture ========================= #

def chop(n=20, mid=80_000.0, pad=100.0):
    """Bars that oscillate enough to touch both a tight bid and ask each bar."""
    return [bar(mid, mid + pad, mid - pad, mid) for _ in range(n)]


def downtrend(n=10, start=80_000.0, step=50.0, dip=150.0):
    """Each bar opens lower and dips (low below bid) but never rallies to the
    ask: a long-only maker just accumulates inventory."""
    bars = []
    p = start
    for _ in range(n):
        o = p
        bars.append(bar(o, o, o - dip, o - step))  # high == open: ask never hit
        p = o - step
    return bars


def test_maker_too_few_bars():
    res = simulate_maker([bar(1, 1, 1, 1)], BacktestConfig())
    assert res.buys == 0 and res.sells == 0


def test_maker_captures_spread_in_chop_zero_fee():
    res = simulate_maker(chop(), BacktestConfig(quote_bps=10.0, maker_fee_bps=0.0))
    assert res.buys > 0 and res.sells > 0
    assert res.spread_captured_usd > 0
    assert res.net_pnl_usd > 0  # spread with no fee is pure profit in chop


def test_maker_fee_above_spread_loses():
    # 4 bps spread vs 20 bps round-trip fee -> no edge to capture.
    res = simulate_maker(chop(), BacktestConfig(quote_bps=4.0, maker_fee_bps=10.0))
    assert res.round_trips > 0
    assert res.net_pnl_usd < 0


def test_maker_adverse_selection_in_downtrend():
    res = simulate_maker(downtrend(), BacktestConfig(quote_bps=4.0, maker_fee_bps=0.0,
                                                     trade_usd=100.0,
                                                     max_inventory_usd=300.0))
    assert res.sells == 0  # ask never filled
    assert res.buys > 0
    assert res.end_inventory_usd > 0  # stuck long
    assert res.end_inventory_mtm_pnl < 0  # marked against us
    assert res.net_pnl_usd < 0
    assert res.max_drawdown_usd < 0


def test_maker_respects_inventory_cap():
    # cap 300, clip 100 -> at most ~4 buys before the cap blocks further fills.
    res = simulate_maker(downtrend(n=30), BacktestConfig(quote_bps=4.0, maker_fee_bps=0.0,
                                                         trade_usd=100.0,
                                                         max_inventory_usd=300.0))
    assert res.buys <= 4


def test_maker_report_renders():
    res = simulate_maker(chop(), BacktestConfig(quote_bps=10.0, maker_fee_bps=0.0))
    txt = format_maker_report(res, "BTC/USD")
    assert "MAKER" in txt
    assert "NET P&L" in txt
    assert "verdict:" in txt


# ================================ TSMOM =================================== #
from jevloop.backtest import TsmomConfig, simulate_tsmom, format_tsmom_report


def closes_to_bars(closes):
    # TSMOM uses closes only; o/h/l are set equal so the helpers stay valid.
    return [bar(c, c, c, c) for c in closes]


def test_tsmom_too_few_bars():
    res = simulate_tsmom(closes_to_bars([100.0] * 5), TsmomConfig(lookback_days=30))
    assert res.days_total == 0


def test_tsmom_rides_an_uptrend_fully_invested():
    # steady uptrend: trailing return always positive -> always long, one entry.
    closes = [100.0 * (1.01 ** i) for i in range(80)]
    res = simulate_tsmom(closes_to_bars(closes),
                         TsmomConfig(lookback_days=10, cost_bps_one_way=15.0))
    assert res.days_total > 0
    assert res.time_in_market > 0.9  # essentially always long
    assert res.trades <= 2  # at most one entry (and maybe exit at the very end)
    # with (almost) full exposure the strategy tracks buy-and-hold closely
    assert res.strat_total_return > 0


def test_tsmom_sidesteps_a_crash_shallower_drawdown():
    # up for 60 days, then a sustained crash: TSMOM should flip to cash and
    # take a much shallower drawdown than buy-and-hold.
    up = [100.0 * (1.01 ** i) for i in range(60)]
    peak = up[-1]
    crash = [peak * (0.97 ** i) for i in range(1, 40)]
    res = simulate_tsmom(closes_to_bars(up + crash),
                         TsmomConfig(lookback_days=10, cost_bps_one_way=15.0))
    # shallower (less negative) drawdown than just holding
    assert res.strat_max_dd > res.hold_max_dd
    # and it spent part of the crash in cash
    assert res.time_in_market < 1.0


def test_tsmom_counts_transitions_and_costs():
    # one clean regime flip: up then down -> expect at least one enter and exit.
    up = [100.0 + i for i in range(40)]
    down = [up[-1] - i for i in range(1, 40)]
    res = simulate_tsmom(closes_to_bars(up + down),
                         TsmomConfig(lookback_days=10, cost_bps_one_way=50.0))
    assert res.trades >= 2
    assert len(res.strat_rets) == res.days_total


def test_tsmom_report_renders():
    closes = [100.0 * (1.005 ** i) for i in range(80)]
    res = simulate_tsmom(closes_to_bars(closes), TsmomConfig(lookback_days=10))
    txt = format_tsmom_report(res, "BTC/USD")
    assert "TSMOM" in txt
    assert "buy & hold" in txt
    assert "max drawdown" in txt


# ============================= walk-forward =============================== #
from jevloop.backtest import (
    WalkForwardConfig, simulate_walkforward, format_walkforward_report,
)


def test_walkforward_too_short():
    res = simulate_walkforward(closes_to_bars([100.0 + i for i in range(50)]),
                               WalkForwardConfig(train_days=180, test_days=45))
    assert res.oos_days == 0


def test_walkforward_produces_oos_segments():
    # ~2 years of a gently trending series: enough for several OOS segments.
    closes = [100.0 * (1.002 ** i) for i in range(700)]
    res = simulate_walkforward(closes_to_bars(closes),
                               WalkForwardConfig(train_days=120, test_days=40))
    assert res.segments >= 3
    assert res.oos_days == res.segments * 40
    assert len(res.picks) == res.segments
    # OOS hold return is just the asset's compounded return over the OOS span
    assert res.hold_total > 0


def test_walkforward_report_mentions_out_of_sample():
    closes = [100.0 * (1.001 ** i) for i in range(500)]
    res = simulate_walkforward(closes_to_bars(closes),
                               WalkForwardConfig(train_days=120, test_days=40))
    txt = format_walkforward_report(res, "BTC/USD")
    assert "OUT-OF-SAMPLE" in txt
    assert "verdict:" in txt


def test_walkforward_noise_has_no_edge():
    # deterministic pseudo-random walk: no real trend -> OOS Sharpe near zero,
    # the honest "no edge" branch of the verdict.
    import math
    closes = [100.0]
    for i in range(1, 600):
        closes.append(closes[-1] * (1 + 0.01 * math.sin(i * 1.7) * math.cos(i * 0.3)))
    res = simulate_walkforward(closes_to_bars(closes),
                               WalkForwardConfig(train_days=120, test_days=40))
    assert res.oos_days > 0
    assert abs(res.strat_sharpe) < 3.0  # no absurd fitted Sharpe out-of-sample
