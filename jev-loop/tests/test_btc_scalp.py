"""Tests for the btc_scalp named strategy in strategy.py.

The scalper is driven entirely by deterministic microstructure fields, so
these tests build plain snapshot dicts and assert on the Action it returns.
They call _btc_scalp directly (no env needed) and also check that
apply_strategy only dispatches to it when JEV_STRATEGY selects it.
"""

from jevloop.policy import (
    Action,
    KILL,
    PULL_QUOTES,
    QUOTE_BOTH_SIDES,
    QUOTE_WIDE,
    STAND_DOWN,
    WIDEN,
)
from jevloop.limits import Limits
from jevloop import strategy
from jevloop.strategy import SCALP, _btc_scalp, _micro_signal, apply_strategy

L = Limits()
NO_ANSWERS: dict = {}


def snap(**over) -> dict:
    base = dict(
        mid=80_000.0,
        microprice=80_000.0,
        spread_bps=3.0,
        imbalance=0.0,
        aggressive_buy_ratio=0.5,
        return_1m=0.0,
        inventory=0.0,
        unrealised_pnl_usd=0.0,
        position_age_s=0.0,
    )
    base.update(over)
    return base


def quoting_action():
    return Action(QUOTE_BOTH_SIDES, reason="env ok")


# --- signal ---------------------------------------------------------------

def test_signal_zero_on_neutral_book():
    assert abs(_micro_signal(snap(), SCALP)) < 1e-9


def test_signal_positive_on_buy_pressure():
    s = _micro_signal(
        snap(microprice=80_010.0, imbalance=0.6, aggressive_buy_ratio=0.8, return_1m=0.002),
        SCALP,
    )
    assert s > 0.5


def test_signal_negative_on_sell_pressure():
    s = _micro_signal(
        snap(microprice=79_990.0, imbalance=-0.6, aggressive_buy_ratio=0.2, return_1m=-0.002),
        SCALP,
    )
    assert s < -0.5


def test_signal_clamped_and_ignores_missing_fields():
    # None fields must contribute 0, not blow up.
    s = _micro_signal({"mid": 80_000.0}, SCALP)
    assert s == 0.0


# --- entry (flat) ---------------------------------------------------------

def test_enter_long_on_strong_buy_pressure():
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(microprice=80_015.0, imbalance=0.7, aggressive_buy_ratio=0.85, return_1m=0.003),
        L,
    )
    assert a.kind == QUOTE_BOTH_SIDES
    assert a.direction_leg == "up"


def test_stand_aside_when_flat_and_bearish():
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(microprice=79_985.0, imbalance=-0.7, aggressive_buy_ratio=0.15, return_1m=-0.003),
        L,
    )
    assert a.kind == STAND_DOWN


def test_weak_signal_quotes_passively_no_leg():
    a = _btc_scalp(quoting_action(), NO_ANSWERS, snap(imbalance=0.05), L)
    assert a.kind == QUOTE_BOTH_SIDES
    assert a.direction_leg is None


# --- exits (long) ---------------------------------------------------------

def test_take_profit_closes_long():
    # +10 bps of notional, above the 8 bps take-profit default.
    inv = 0.01
    mid = 80_000.0
    upnl = 10 / 1e4 * inv * mid
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(inventory=inv, unrealised_pnl_usd=upnl, position_age_s=5.0),
        L,
    )
    assert a.direction_leg == "down"
    assert "take profit" in a.reason


def test_soft_stop_closes_long():
    inv = 0.01
    mid = 80_000.0
    upnl = -7 / 1e4 * inv * mid  # -7 bps, below -6 bps soft stop
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(inventory=inv, unrealised_pnl_usd=upnl),
        L,
    )
    assert a.direction_leg == "down"
    assert "soft stop" in a.reason


def test_max_hold_closes_long():
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(inventory=0.01, unrealised_pnl_usd=0.0, position_age_s=300.0),
        L,
    )
    assert a.direction_leg == "down"
    assert "max hold" in a.reason


def test_signal_flip_closes_long():
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(inventory=0.01, microprice=79_980.0, imbalance=-0.6,
             aggressive_buy_ratio=0.2, return_1m=-0.002),
        L,
    )
    assert a.direction_leg == "down"
    assert "flipped" in a.reason


def test_holding_long_does_not_pyramid():
    # Strong buy pressure while already long: hold, no up-leg.
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(inventory=0.01, microprice=80_015.0, imbalance=0.7,
             aggressive_buy_ratio=0.85, return_1m=0.003, position_age_s=5.0),
        L,
    )
    assert a.direction_leg is None
    assert "holding long" in a.reason


# --- spread gate ----------------------------------------------------------

def test_wide_spread_stands_aside_when_flat():
    a = _btc_scalp(quoting_action(), NO_ANSWERS, snap(spread_bps=25.0), L)
    assert a.kind == STAND_DOWN
    assert "spread" in a.reason


def test_wide_spread_exits_when_long():
    a = _btc_scalp(
        quoting_action(), NO_ANSWERS,
        snap(spread_bps=25.0, inventory=0.01),
        L,
    )
    assert a.direction_leg == "down"


# --- safety: never loosen Jev's vetoes ------------------------------------

def test_never_overrides_kill():
    a = _btc_scalp(Action(KILL, reason="drawdown"), NO_ANSWERS, snap(inventory=0.01), L)
    assert a.kind == KILL


def test_never_overrides_pull_quotes():
    a = _btc_scalp(Action(PULL_QUOTES, reason="toxic"), NO_ANSWERS,
                   snap(microprice=80_015.0, imbalance=0.7), L)
    assert a.kind == PULL_QUOTES


def test_never_overrides_widen():
    a = _btc_scalp(Action(WIDEN, reason="stressed"), NO_ANSWERS, snap(), L)
    assert a.kind == WIDEN


# --- dispatch -------------------------------------------------------------

def test_apply_strategy_is_noop_by_default(monkeypatch):
    monkeypatch.delenv("JEV_STRATEGY", raising=False)
    act = Action(QUOTE_BOTH_SIDES, reason="x", direction_leg="up")
    out = apply_strategy(act, NO_ANSWERS,
                         snap(microprice=79_985.0, imbalance=-0.7), L)
    assert out is act  # unchanged object, scalper did not run


def test_apply_strategy_dispatches_to_scalp_when_selected(monkeypatch):
    monkeypatch.setenv("JEV_STRATEGY", "btc_scalp")
    out = apply_strategy(
        Action(QUOTE_BOTH_SIDES, reason="x"), NO_ANSWERS,
        snap(microprice=79_985.0, imbalance=-0.7, aggressive_buy_ratio=0.15,
             return_1m=-0.003),
        L,
    )
    assert out.kind == STAND_DOWN  # scalper ran and stood aside on bearish flat
