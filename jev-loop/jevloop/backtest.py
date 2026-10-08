"""backtest.py -- replay the active scalper over historical 1-minute bars.

What this does. Pulls 1m bars from Alpaca (the same source the loop
backfills from), rebuilds a per-bar deterministic snapshot, and runs the
*actual* strategy hook (`strategy._btc_scalp`) on each bar, long/flat, with
a realistic taker cost model (fee + half-spread each side). It reports
trade count, hit-rate, gross vs net basis points, net P&L on a fixed
per-trade notional, max drawdown and time-in-market.

What it honestly cannot do. The scalper's edge, such as it is, lives in
order-book microstructure -- microprice tilt, book imbalance, aggressive-
buy ratio. None of that exists in historical OHLC bars. This replay
therefore drives the scalper on the **momentum component only** (1-minute
return), optionally plus a candle-body proxy for buy/sell pressure
(`--body-proxy`, clearly a proxy). So:

    This is a cost-and-behaviour check, not a validation of the live
    strategy. A profit here would not prove a live edge, and -- because
    taker fees usually dwarf an 8 bps take-profit -- a loss here is the
    expected, instructive result.

No orders are ever sent. Nothing here touches the trading API.

Decisions use data through each bar's close; fills happen at the *next*
bar's open, so there is no same-bar look-ahead.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import dataclass, field

from .limits import Limits
from .policy import QUOTE_BOTH_SIDES, Action
from .strategy import SCALP, _btc_scalp


# --------------------------------------------------------------------------- #
# cost model + config
# --------------------------------------------------------------------------- #
@dataclass
class BacktestConfig:
    fee_bps_per_side: float = 15.0  # Alpaca crypto taker fee, retail tier ~0.15%
    spread_bps: float = 2.0  # assumed round-trip spread (half crossed each side)
    trade_usd: float = 100.0  # notional per scalp, for the $ P&L column
    body_proxy: bool = False  # use (close-open)/(high-low) as a buy-pressure proxy

    @property
    def cost_bps_per_side(self) -> float:
        return self.fee_bps_per_side + self.spread_bps / 2.0


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    entry_px: float  # cost-adjusted fill
    exit_px: float  # cost-adjusted fill
    gross_bps: float  # price move, no costs
    net_bps: float  # after both-side costs
    pnl_usd: float
    reason: str


@dataclass
class BacktestResult:
    n_bars: int
    trades: list[Trade] = field(default_factory=list)
    equity_usd: list[float] = field(default_factory=list)  # cumulative net P&L curve
    cfg: BacktestConfig = field(default_factory=BacktestConfig)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_bps > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_trades if self.trades else 0.0

    @property
    def net_pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def gross_bps_total(self) -> float:
        return sum(t.gross_bps for t in self.trades)

    @property
    def net_bps_total(self) -> float:
        return sum(t.net_bps for t in self.trades)

    @property
    def avg_net_bps(self) -> float:
        return self.net_bps_total / self.n_trades if self.trades else 0.0

    @property
    def fees_bps_total(self) -> float:
        return self.gross_bps_total - self.net_bps_total

    @property
    def max_drawdown_usd(self) -> float:
        peak = 0.0
        dd = 0.0
        for e in self.equity_usd:
            peak = max(peak, e)
            dd = min(dd, e - peak)
        return dd

    @property
    def bars_in_market(self) -> int:
        return sum(t.exit_i - t.entry_i for t in self.trades)

    @property
    def exposure(self) -> float:
        return self.bars_in_market / self.n_bars if self.n_bars else 0.0


# --------------------------------------------------------------------------- #
# snapshot reconstruction from a bar
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _bar_snapshot(bars: list[dict], i: int, inv: float, entry_px: float,
                  entry_i: int, cfg: BacktestConfig) -> dict:
    cur = bars[i]
    prev_close = bars[i - 1]["c"]
    mid = cur["c"]
    ret_1m = (mid - prev_close) / prev_close if prev_close else 0.0

    abr = None
    if cfg.body_proxy:
        rng = cur["h"] - cur["l"]
        body = (cur["c"] - cur["o"]) / rng if rng else 0.0
        abr = 0.5 + 0.5 * _clamp(body)  # proxy only: candle body -> buy pressure

    return {
        "mid": mid,
        "microprice": mid,  # no book in history -> tilt is 0
        "spread_bps": cfg.spread_bps,
        "imbalance": None,  # no book in history
        "aggressive_buy_ratio": abr,  # None unless --body-proxy
        "return_1m": ret_1m,
        "inventory": inv,
        "unrealised_pnl_usd": (mid - entry_px) * inv if inv else 0.0,
        "position_age_s": (i - entry_i) * 60.0 if inv else 0.0,
    }


# --------------------------------------------------------------------------- #
# the simulation (pure, no network -- this is what the tests exercise)
# --------------------------------------------------------------------------- #
def simulate(bars: list[dict], cfg: BacktestConfig | None = None) -> BacktestResult:
    """Run the scalper over `bars` (each a dict with float o/h/l/c). Decisions
    use data through bar i's close; fills are at bar i+1's open."""
    cfg = cfg or BacktestConfig()
    limits = Limits()
    res = BacktestResult(n_bars=len(bars), cfg=cfg)
    if len(bars) < 3:
        return res

    cost = cfg.cost_bps_per_side / 1e4
    inv = 0.0
    entry_fill = 0.0  # cost-adjusted entry price
    entry_raw = 0.0  # uncosted entry price (for gross bps)
    entry_i = 0
    qty = 0.0
    cum = 0.0

    # decide on bar i, fill on bar i+1's open; stop one short so i+1 exists
    for i in range(1, len(bars) - 1):
        snap = _bar_snapshot(bars, i, inv, entry_fill, entry_i, cfg)
        action = _btc_scalp(Action(QUOTE_BOTH_SIDES, reason="bt"), {}, snap, limits)
        leg = getattr(action, "direction_leg", None)
        fill_open = bars[i + 1]["o"]

        if inv == 0.0 and leg == "up":
            entry_raw = fill_open
            entry_fill = fill_open * (1 + cost)
            qty = cfg.trade_usd / entry_fill
            inv = qty
            entry_i = i + 1
        elif inv > 0.0 and leg == "down":
            exit_raw = fill_open
            exit_fill = fill_open * (1 - cost)
            gross_bps = (exit_raw - entry_raw) / entry_raw * 1e4
            net_bps = (exit_fill - entry_fill) / entry_fill * 1e4
            pnl = qty * (exit_fill - entry_fill)
            cum += pnl
            res.trades.append(Trade(
                entry_i=entry_i, exit_i=i + 1, entry_px=entry_fill, exit_px=exit_fill,
                gross_bps=gross_bps, net_bps=net_bps, pnl_usd=pnl, reason=action.reason,
            ))
            res.equity_usd.append(cum)
            inv = 0.0
            qty = 0.0

    # mark-to-market any open position at the last close (flagged in reason)
    if inv > 0.0:
        last = bars[-1]["c"]
        exit_fill = last * (1 - cost)
        gross_bps = (last - entry_raw) / entry_raw * 1e4
        net_bps = (exit_fill - entry_fill) / entry_fill * 1e4
        pnl = qty * (exit_fill - entry_fill)
        cum += pnl
        res.trades.append(Trade(
            entry_i=entry_i, exit_i=len(bars) - 1, entry_px=entry_fill, exit_px=exit_fill,
            gross_bps=gross_bps, net_bps=net_bps, pnl_usd=pnl,
            reason="forced close at end of data",
        ))
        res.equity_usd.append(cum)

    return res


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def format_report(res: BacktestResult, symbol: str) -> str:
    cfg = res.cfg
    L = []
    L.append("=" * 64)
    L.append(f"BACKTEST  {symbol}  (btc_scalp, replay on 1m bars)")
    L.append("=" * 64)
    L.append("HONEST SCOPE: book microstructure (imbalance, microprice tilt,")
    L.append("trade flow) is not in historical bars. This drives the scalper on")
    L.append("the momentum component" + (" + candle-body proxy" if cfg.body_proxy else "")
             + " only. Cost/behaviour check, not an edge test.")
    L.append("-" * 64)
    L.append(f"bars replayed:        {res.n_bars}")
    L.append(f"cost model:           {cfg.fee_bps_per_side:.1f} bps fee/side + "
             f"{cfg.spread_bps:.1f} bps spread = "
             f"{2 * cfg.cost_bps_per_side:.1f} bps round-trip")
    L.append(f"notional per trade:   ${cfg.trade_usd:,.2f}")
    L.append("-" * 64)
    if res.n_trades == 0:
        L.append("no trades: the momentum signal never cleared the entry threshold")
        L.append("on this window. Try more bars, --body-proxy, or a lower")
        L.append("enter_signal in strategy.py (more churn, not more edge).")
        L.append("=" * 64)
        return "\n".join(L)
    L.append(f"trades:               {res.n_trades}")
    L.append(f"win rate:             {res.win_rate:.1%}  ({res.wins}W / "
             f"{res.n_trades - res.wins}L)")
    L.append(f"gross (pre-cost):     {res.gross_bps_total:+.1f} bps total, "
             f"{res.gross_bps_total / res.n_trades:+.2f} bps/trade")
    L.append(f"costs:                {res.fees_bps_total:.1f} bps total")
    L.append(f"net (after cost):     {res.net_bps_total:+.1f} bps total, "
             f"{res.avg_net_bps:+.2f} bps/trade")
    L.append(f"net P&L:              ${res.net_pnl_usd:+,.2f}")
    L.append(f"max drawdown:         ${res.max_drawdown_usd:,.2f}")
    L.append(f"time in market:       {res.exposure:.1%} of bars")
    L.append("-" * 64)
    verdict = ("net positive on this window, BUT see the honest scope above -- "
               "momentum-only, one window, not an edge.") if res.net_pnl_usd > 0 else (
        "net negative -- the usual result: taker costs "
        f"({2 * cfg.cost_bps_per_side:.0f} bps round-trip) dwarf the "
        f"{SCALP.take_profit_bps:.0f} bps take-profit.")
    L.append("verdict: " + verdict)
    L.append("=" * 64)
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _normalise_bars(raw: list[dict]) -> list[dict]:
    out = []
    for b in raw:
        try:
            out.append({"o": float(b["o"]), "h": float(b["h"]),
                        "l": float(b["l"]), "c": float(b["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="jev-loop backtest")
    ap.add_argument("--symbol", default="BTC/USD")
    ap.add_argument("--minutes", type=int, default=1000,
                    help="how many 1-minute bars back to replay (max ~1000/req)")
    ap.add_argument("--fee-bps", type=float, default=15.0,
                    help="taker fee per side, bps (Alpaca crypto retail ~15)")
    ap.add_argument("--spread-bps", type=float, default=2.0,
                    help="assumed round-trip spread, bps")
    ap.add_argument("--trade-usd", type=float, default=100.0)
    ap.add_argument("--body-proxy", action="store_true",
                    help="use candle body as a buy-pressure proxy (clearly a proxy)")
    args = ap.parse_args(argv)

    cfg = BacktestConfig(
        fee_bps_per_side=args.fee_bps, spread_bps=args.spread_bps,
        trade_usd=args.trade_usd, body_proxy=args.body_proxy,
    )

    try:
        from .execution.alpaca import AlpacaConfigError, client_from_env
    except Exception as exc:  # pragma: no cover
        print(f"cannot import execution client: {exc}")
        return 1

    try:
        client = client_from_env(symbol=args.symbol)
    except AlpacaConfigError as exc:
        print(f"cannot start backtest: {exc}")
        print("set ALPACA_API_KEY / ALPACA_SECRET_KEY (paper) first.")
        return 1

    minutes = max(3, min(args.minutes, 10000))
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes + 5)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"fetching ~{minutes} one-minute bars for {args.symbol} since {start_iso} ...")
    try:
        raw = client.get_minute_bars(start_iso, limit=min(minutes + 5, 1000))
    except Exception as exc:
        print(f"bar fetch failed: {exc}")
        return 1

    bars = _normalise_bars(raw)
    if len(bars) < 3:
        print(f"only {len(bars)} usable bars returned; need at least 3. "
              "Try a larger --minutes.")
        return 1

    res = simulate(bars, cfg)
    print(format_report(res, args.symbol))
    return 0
