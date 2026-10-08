"""compose_action(): the policy engine. Yours in code forever.

The seven thresholds this reads come from strategy.py, the file you edit
to change how the loop trades: see StrategyThresholds and the
apply_strategy() hook there. This function never calls Jev. It only reads
the seven answers Jev already gave this tick. When the model gets faster,
cheaper, or replaced, this file does not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .limits import Limits
from .strategy import THRESHOLDS, apply_strategy

KILL = "KILL"
PULL_QUOTES = "PULL_QUOTES"
WIDEN = "WIDEN"
QUOTE_BOTH_SIDES = "QUOTE_BOTH_SIDES"
QUOTE_WIDE = "QUOTE_WIDE"
STAND_DOWN = "STAND_DOWN"


@dataclass
class Action:
    kind: str
    reason: str
    skew: float = 0.0
    direction_leg: str | None = None  # "up" | "down" | None


def inventory_skew(pressure_score: float, max_score: float, inventory: float) -> float:
    """Signed skew in [-1, 1]. Negative skews toward selling (cutting a long),
    positive skews toward buying (cutting a short). Zero inventory -> no skew."""
    if inventory == 0:
        return 0.0
    magnitude = max(0.0, min(1.0, pressure_score / max_score))
    return -magnitude if inventory > 0 else magnitude


def compose_action(answers: dict, snapshot: dict, limits: Limits) -> Action:
    """Composes an action from THRESHOLDS in strategy.py, then hands it to
    strategy.apply_strategy() for one last look before it goes anywhere
    near an order. That hook can change the action or veto it; risk.py
    still runs after that and still has the final word."""
    action = _compose_from_thresholds(answers, snapshot, limits)
    return apply_strategy(action, answers, snapshot, limits)


def _compose_from_thresholds(answers: dict, snapshot: dict, limits: Limits) -> Action:
    if snapshot["drawdown_pct"] > limits.max_drawdown_pct:
        return Action(
            KILL, reason=f"drawdown {snapshot['drawdown_pct']:.2%} over limit"
        )

    toxic = answers["toxic_flow"]["noul"]
    if toxic > THRESHOLDS.toxic_flow_pull_threshold:
        return Action(PULL_QUOTES, reason=f"toxic flow {toxic:.2f}")

    liquidity_stressed = answers["liquidity_stressed"]["noul"]
    if liquidity_stressed > THRESHOLDS.liquidity_stressed_widen_threshold:
        return Action(WIDEN, reason=f"liquidity stressed {liquidity_stressed:.2f}")

    q = answers["quote_environment"]
    inv_p = answers["inventory_pressure"]

    if (
        q["score"] >= THRESHOLDS.quote_env_full_score
        and q["confidence"] > THRESHOLDS.quote_env_full_confidence
    ):
        skew = inventory_skew(
            inv_p["score"],
            THRESHOLDS.inventory_pressure_max_score,
            snapshot["inventory"],
        )
        action = Action(
            QUOTE_BOTH_SIDES,
            skew=skew,
            reason=f"env {q['score']:.2f} conf {q['confidence']:.2f}",
        )
    elif q["score"] >= THRESHOLDS.quote_env_wide_score:
        action = Action(QUOTE_WIDE, reason=f"env {q['score']:.2f}")
    else:
        action = Action(STAND_DOWN, reason=f"env {q['score']:.2f} below quoting floor")

    # Directional leg: bolted onto a quoting action so the demo shows fills
    # constantly, not just resting quotes. Never overrides a KILL/PULL/WIDEN.
    if action.kind in (QUOTE_BOTH_SIDES, QUOTE_WIDE):
        direction = answers["direction"]
        if (
            direction["choice"] != "neutral"
            and direction["confidence"] > THRESHOLDS.direction_confidence_threshold
        ):
            action.direction_leg = direction["choice"]

    return action


def fallback_action(snapshot: dict, limits: Limits) -> Action:
    """Deterministic, Jev-free policy used on the RULES_ONLY rung (Jev down
    or unavailable). Uses only spread and imbalance, nothing that needed a
    judgment call. This is what keeps the system honestly '24/7' rather than
    '24/7 until the model has a bad day'."""
    if snapshot["drawdown_pct"] > limits.max_drawdown_pct:
        return Action(KILL, reason="drawdown breach (rules-only)")
    if snapshot["spread_bps"] > 15.0:
        return Action(STAND_DOWN, reason="spread too wide for rules-only quoting")
    imbalance = snapshot.get("imbalance")
    if imbalance is not None and abs(imbalance) > 0.6:
        return Action(WIDEN, reason="book imbalance too high for rules-only quoting")
    return Action(QUOTE_WIDE, reason="rules-only: spread and imbalance both acceptable")
