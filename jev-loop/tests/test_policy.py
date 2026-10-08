from jevloop.limits import Limits
from jevloop.policy import (
    KILL,
    PULL_QUOTES,
    QUOTE_BOTH_SIDES,
    QUOTE_WIDE,
    STAND_DOWN,
    WIDEN,
    compose_action,
    fallback_action,
    inventory_skew,
)

L = Limits()

BASE_SNAPSHOT = dict(
    drawdown_pct=0.01,
    inventory=0.0,
    daily_loss_usd=0.0,
    position_age_s=0.0,
    data_age_s=0.1,
    leverage=1.0,
    spread_bps=4.0,
    imbalance=0.0,
)

BASE_ANSWERS = {
    "toxic_flow": {"noul": 0.2},
    "liquidity_stressed": {"noul": 0.1},
    "quote_environment": {"score": 2.3, "confidence": 0.84},
    "inventory_pressure": {"score": 1.1},
    "direction": {"choice": "neutral", "confidence": 0.9},
}


def test_kill_on_drawdown_breach():
    snap = dict(BASE_SNAPSHOT, drawdown_pct=0.2)
    action = compose_action(BASE_ANSWERS, snap, L)
    assert action.kind == KILL


def test_pull_quotes_on_toxic_flow():
    answers = dict(BASE_ANSWERS, toxic_flow={"noul": 0.75})
    action = compose_action(answers, BASE_SNAPSHOT, L)
    assert action.kind == PULL_QUOTES


def test_widen_on_liquidity_stress():
    answers = dict(BASE_ANSWERS, liquidity_stressed={"noul": 0.85})
    action = compose_action(answers, BASE_SNAPSHOT, L)
    assert action.kind == WIDEN


def test_quote_both_sides_matches_article_example():
    # env 2.3, confidence 0.84 -- the article's own worked example.
    action = compose_action(BASE_ANSWERS, BASE_SNAPSHOT, L)
    assert action.kind == QUOTE_BOTH_SIDES


def test_quote_wide_below_full_confidence():
    answers = dict(BASE_ANSWERS, quote_environment={"score": 2.3, "confidence": 0.5})
    action = compose_action(answers, BASE_SNAPSHOT, L)
    assert action.kind == QUOTE_WIDE


def test_stand_down_below_quoting_floor():
    answers = dict(BASE_ANSWERS, quote_environment={"score": 0.5, "confidence": 0.9})
    action = compose_action(answers, BASE_SNAPSHOT, L)
    assert action.kind == STAND_DOWN


def test_directional_leg_only_when_confident_and_quoting():
    answers = dict(BASE_ANSWERS, direction={"choice": "up", "confidence": 0.9})
    action = compose_action(answers, BASE_SNAPSHOT, L)
    assert action.kind == QUOTE_BOTH_SIDES
    assert action.direction_leg == "up"

    low_conf = dict(BASE_ANSWERS, direction={"choice": "up", "confidence": 0.1})
    action2 = compose_action(low_conf, BASE_SNAPSHOT, L)
    assert action2.direction_leg is None


def test_directional_leg_never_fires_on_pull_or_widen():
    answers = dict(
        BASE_ANSWERS,
        toxic_flow={"noul": 0.9},
        direction={"choice": "up", "confidence": 0.99},
    )
    action = compose_action(answers, BASE_SNAPSHOT, L)
    assert action.kind == PULL_QUOTES
    assert action.direction_leg is None


def test_inventory_skew_direction():
    # Long inventory should skew toward selling (negative).
    assert inventory_skew(3.0, 3.0, inventory=0.01) < 0
    # Short inventory should skew toward buying (positive).
    assert inventory_skew(3.0, 3.0, inventory=-0.01) > 0
    # Flat inventory: no skew regardless of pressure.
    assert inventory_skew(3.0, 3.0, inventory=0.0) == 0.0


def test_fallback_action_never_touches_jev():
    # Confirms fallback_action's signature takes no `answers` argument at all --
    # a structural guarantee that the rules-only rung cannot call the model.
    import inspect

    sig = inspect.signature(fallback_action)
    assert "answers" not in sig.parameters

    action = fallback_action(BASE_SNAPSHOT, L)
    assert action.kind in (KILL, STAND_DOWN, WIDEN, QUOTE_WIDE)
