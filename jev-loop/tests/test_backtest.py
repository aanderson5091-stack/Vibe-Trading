"""Tests for the bar-replay backtester. All synthetic bars, no network."""

from jevloop.backtest import BacktestConfig, simulate, format_report, _normalise_bars


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
