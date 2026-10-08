"""strategy.py -- the file you edit to change how this loop trades.

This is the harness the video talks about, not the edge. Everything it
shipped with is a generic strategy pulled out of thin air, wired in only
so the demo has something to trade. Real strategies are hard to build
properly; this file is where yours goes.

Two things live here:

1. `StrategyThresholds`, one number per action in `compose_action()`
   (policy.py): when to pull quotes, when to widen, when the quote
   environment is good enough to quote both sides or just quote wide, how
   much inventory pressure skews sizing, and how confident Jev has to be
   about direction before a directional leg is taken. Change a number,
   restart the loop, see different behaviour on the next tick.

2. `apply_strategy()`, a hook called once per tick with the action
   `compose_action()` already produced from the thresholds above. Return
   it unchanged (the default) and nothing changes from what shipped in
   the video. Return a different action to override it, or an action
   with `kind=STAND_DOWN` to veto the tick outright. This is the one
   function a real strategy plugs into.

Shipped default: every threshold below matches what the video ran, and
`apply_strategy()` is a no-op UNLESS you select a named strategy with the
`JEV_STRATEGY` environment variable (see "Named strategies" below).
Nothing changes for someone who never opens this file and never sets that
variable.

The hard risk caps (max position, max daily loss, max drawdown, and so
on) do NOT live here: they live in `jevloop/limits.py`, checked in
`risk.py` before every order, and this file cannot raise them. A
strategy can make the loop more conservative than the risk engine
allows; it can never make it less conservative than the risk engine
allows.

Named strategies
----------------
`apply_strategy()` dispatches on the `JEV_STRATEGY` environment variable:

    JEV_STRATEGY unset / "default"   -> no-op, the shipped behaviour
    JEV_STRATEGY=btc_scalp           -> the microstructure scalper below

Set it in your `.env` (JEV_STRATEGY=btc_scalp) and restart the loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class StrategyThresholds:
    """One threshold per action in compose_action(). These are exactly the
    numbers the video shipped with."""

    toxic_flow_pull_threshold: float = 0.6  # ans["toxic_flow"] > this -> PULL_QUOTES
    liquidity_stressed_widen_threshold: float = (
        0.7  # ans["liquidity_stressed"] > this -> WIDEN
    )
    quote_env_full_score: float = 2.0  # env >= this and confident -> quote both sides
    quote_env_full_confidence: float = 0.80
    quote_env_wide_score: float = 1.0  # env >= this (but below full) -> quote wide
    inventory_pressure_max_score: float = 3.0  # denominator for the skew calculation

    # the directional leg, bolted on so the demo shows fills, not just quotes
    direction_confidence_threshold: float = (
        0.55  # direction.confidence above this -> take the leg
    )


THRESHOLDS = StrategyThresholds()


# ---------------------------------------------------------------------------
# Named strategy: BTC microstructure scalper  (JEV_STRATEGY=btc_scalp)
# ---------------------------------------------------------------------------
#
# What this is, honestly. This is NOT high-frequency trading in the
# co-located, sub-millisecond sense -- that is impossible from a retail
# Alpaca REST account (a ~2s tick, ~90 calls/min, taker market orders, and
# retail fees). It is a *high-frequency-flavoured* scalper: it reacts every
# tick to order-book microstructure and takes small, short-held long
# positions, long-or-flat (Alpaca crypto cannot short -- a "down" leg just
# closes the long).
#
# What drives it. Only the DETERMINISTIC snapshot (state.py), never the Jev
# numbers -- so it behaves identically whether you run a real Jev key or the
# mock. The signal is a weighted blend of four code-computed microstructure
# reads, each normalised to roughly [-1, 1]:
#
#   * microprice tilt   (microprice - mid): size-weighted book pressure
#   * book imbalance     bid vs ask depth
#   * aggressive-buy ratio: share of recent trades that lifted the offer
#   * 1-minute return    short momentum
#
# What it does NOT promise. An edge. This is a test harness for the loop's
# entry/exit/inventory plumbing. Taker scalping through a spread with fees
# is structurally hard to make profitable; expect it to churn. Tune the
# numbers below, or replace the signal entirely, once you can see it trade.
#
# Risk. Everything here can only ADD caution. It never touches KILL /
# PULL_QUOTES / WIDEN (Jev's own safety vetoes), never adds to a position,
# and risk.py / limits.py still have the final word on every order.


@dataclass
class ScalpParams:
    """Tunables for the btc_scalp strategy. Edit, restart, observe."""

    max_spread_bps: float = 10.0  # above this the spread eats the scalp -> stand aside
    enter_signal: float = 0.35  # |signal| at/above this opens a long (if bullish)
    exit_signal: float = 0.15  # signal at/below -this closes a held long
    take_profit_bps: float = 8.0  # close the long once it is this far in the money
    soft_stop_bps: float = 6.0  # close the long once it is this far offside
    max_hold_s: float = 120.0  # a scalp is quick; flatten after this many seconds
    # signal normalisation ("full" = the value that counts as a maxed-out read)
    tilt_full_bps: float = 3.0
    mom_full: float = 0.0015  # 0.15% over 1m is a full momentum read
    imb_full: float = 0.5
    abr_full: float = 0.25  # aggressive-buy ratio 0.5 +/- this is a full read
    # signal blend weights (need not sum to 1; the result is clamped anyway)
    w_tilt: float = 0.35
    w_imb: float = 0.25
    w_abr: float = 0.20
    w_mom: float = 0.20
    # optional: let a real Jev regime read add caution (no effect under the mock
    # unless you have a key; off by default so the test is pure microstructure)
    use_jev_confirm: bool = False


SCALP = ScalpParams()

_SCALP_NAMES = {"btc_scalp", "btc-scalp", "scalp", "hft", "btc"}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _micro_signal(snapshot: dict, p: ScalpParams) -> float:
    """Composite directional pressure in [-1, 1], positive = buy pressure.
    Computed only from deterministic snapshot fields. Missing/None fields
    contribute 0 rather than a fabricated reading."""
    mid = snapshot.get("mid") or 0.0
    micro = snapshot.get("microprice") or mid

    tilt_bps = ((micro - mid) / mid * 1e4) if mid else 0.0
    s_tilt = _clamp(tilt_bps / p.tilt_full_bps) if p.tilt_full_bps else 0.0

    imb = snapshot.get("imbalance")
    s_imb = _clamp((imb or 0.0) / p.imb_full) if p.imb_full else 0.0

    abr = snapshot.get("aggressive_buy_ratio")
    s_abr = _clamp(((abr - 0.5) / p.abr_full)) if (abr is not None and p.abr_full) else 0.0

    r1 = snapshot.get("return_1m")
    s_mom = _clamp((r1 or 0.0) / p.mom_full) if p.mom_full else 0.0

    raw = p.w_tilt * s_tilt + p.w_imb * s_imb + p.w_abr * s_abr + p.w_mom * s_mom
    return _clamp(raw)


def _upnl_bps(snapshot: dict) -> float:
    """Unrealised PnL on the held position, in basis points of notional."""
    inv = snapshot.get("inventory") or 0.0
    mid = snapshot.get("mid") or 0.0
    notional = abs(inv) * mid
    if notional <= 0:
        return 0.0
    return (snapshot.get("unrealised_pnl_usd") or 0.0) / notional * 1e4


def _btc_scalp(action, answers: dict, snapshot: dict, limits) -> object:
    """Microstructure scalper hook. See the section comment above."""
    Action = action.__class__  # avoid a circular import of policy at module load
    kind = action.kind
    p = SCALP

    # Never loosen Jev's own safety vetoes (KILL / PULL_QUOTES / WIDEN).
    if kind in ("KILL", "PULL_QUOTES", "WIDEN"):
        return action

    quoting = kind in ("QUOTE_BOTH_SIDES", "QUOTE_WIDE")
    inv = snapshot.get("inventory") or 0.0
    spread = snapshot.get("spread_bps")
    sig = _micro_signal(snapshot, p)

    # Optional regime guard (only if a real Jev key is wired AND you opt in).
    if p.use_jev_confirm and inv <= 0:
        regime = (answers or {}).get("regime", {})
        if regime.get("choice") in ("crisis", "high_vol"):
            return Action("STAND_DOWN", reason=f"scalp: regime {regime.get('choice')}")

    # Spread gate: too wide to scalp with taker legs.
    if spread is not None and spread > p.max_spread_bps:
        if inv > 0 and quoting:
            return Action(
                kind,
                reason=f"scalp exit: spread {spread:.1f}bps too wide to hold",
                skew=action.skew,
                direction_leg="down",
            )
        return Action(
            "STAND_DOWN", reason=f"scalp: spread {spread:.1f}bps > {p.max_spread_bps:.0f}"
        )

    # --- Holding a long: manage the exit, never add. ---
    if inv > 0:
        upnl = _upnl_bps(snapshot)
        age = snapshot.get("position_age_s") or 0.0
        exit_reason = None
        if upnl >= p.take_profit_bps:
            exit_reason = f"take profit {upnl:+.1f}bps"
        elif upnl <= -p.soft_stop_bps:
            exit_reason = f"soft stop {upnl:+.1f}bps"
        elif age >= p.max_hold_s:
            exit_reason = f"max hold {age:.0f}s"
        elif sig <= -p.exit_signal:
            exit_reason = f"signal flipped {sig:+.2f}"

        if exit_reason and quoting:
            return Action(
                kind, reason=f"scalp exit: {exit_reason}", skew=action.skew,
                direction_leg="down",
            )
        if quoting:  # hold: keep quoting, strip any up-leg so we don't pyramid
            return Action(
                kind, reason=f"scalp: holding long upnl {upnl:+.1f}bps, no add",
                skew=action.skew, direction_leg=None,
            )
        return action

    # --- Flat: open a long only on strong buy pressure (long-only venue). ---
    if quoting:
        if sig >= p.enter_signal:
            return Action(
                kind, reason=f"scalp enter long sig {sig:+.2f}", skew=action.skew,
                direction_leg="up",
            )
        if sig <= -p.enter_signal:
            return Action(
                "STAND_DOWN",
                reason=f"scalp: bearish sig {sig:+.2f}, long-only, standing aside",
            )
        return Action(
            kind, reason=f"scalp: weak sig {sig:+.2f}, passive quotes",
            skew=action.skew, direction_leg=None,
        )
    return action


def _active_strategy() -> str:
    return os.getenv("JEV_STRATEGY", "default").strip().lower()


def apply_strategy(action, answers: dict, snapshot: dict, limits) -> object:
    """The strategy hook. Called once per tick, after compose_action() has
    already turned Jev's answers into an action using THRESHOLDS above.

    Dispatches on the JEV_STRATEGY environment variable:
      - unset / "default": return the action unchanged (the shipped no-op).
      - "btc_scalp": the microstructure scalper (_btc_scalp) above.

    `risk.py` still runs after this and still has the final veto, so
    nothing returned here can bypass a hard limit in limits.py, only add
    more caution on top of it.
    """
    if _active_strategy() in _SCALP_NAMES:
        return _btc_scalp(action, answers, snapshot, limits)
    return action
