"""The five-rung fallback ladder.

This is what makes "24/7" true past 2am, when the wifi drops or Jev has a
bad night. The loop always lands on exactly one rung per tick, and the rung
(not the model) decides whether to trade, shrink, hold, or flatten.

    healthy + high confidence  -> RUN         (normal operation)
    healthy + low confidence   -> REDUCE      (smaller size)
    late past block deadline   -> HOLD_LATE   (no stale quotes, ever)
    Jev unavailable            -> RULES_ONLY  (deterministic fallback only)
    hard limit breached        -> KILL        (flatten, stop, alert)
"""

from __future__ import annotations

from enum import Enum


class Rung(str, Enum):
    RUN = "run"
    REDUCE = "reduce"
    HOLD_LATE = "hold_late"
    RULES_ONLY = "rules_only"
    KILL = "kill"


def select_rung(
    *,
    risk_kill: bool,
    decision_late: bool,
    jev_down: bool,
    decision_confidence: float | None,
    low_confidence_threshold: float,
    execution_health_score: float | None = None,
    execution_health_floor: float = 1.0,
) -> Rung:
    """decision_confidence is the confidence of whichever answer the policy
    engine actually used to decide (quote_environment.confidence in the
    common case). None when no decision was made at all (late or down).

    execution_health_score is Jev's own execution_health judgment for this
    tick (0 = Broken, 3 = Optimal). Below execution_health_floor, the ladder
    treats the tick the same way it treats low confidence: REDUCE, never
    RUN. This is the one place execution_health feeds back into behaviour,
    not just the log."""
    if risk_kill:
        return Rung.KILL
    if decision_late:
        return Rung.HOLD_LATE
    if jev_down:
        return Rung.RULES_ONLY
    if (
        decision_confidence is not None
        and decision_confidence < low_confidence_threshold
    ):
        return Rung.REDUCE
    if (
        execution_health_score is not None
        and execution_health_score < execution_health_floor
    ):
        return Rung.REDUCE
    return Rung.RUN
