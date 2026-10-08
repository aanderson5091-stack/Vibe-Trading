"""The risk engine. Nine hard limits, checked before every single order.

Never delegates to Jev. Every limit here is checkable from something other
than the model's own claim: a number in the snapshot, a counter the loop
itself keeps. A KILL verdict means flatten and stop, not "ask the model
whether it's really that bad".
"""

from __future__ import annotations

from dataclasses import dataclass

from .limits import Limits


@dataclass
class RiskVerdict:
    ok: bool
    veto: str | None = None
    kill: bool = False


def check(
    snapshot: dict,
    order_notional_usd: float,
    limits: Limits,
    api_error_streak: int,
    decision_latency_ms: float | None,
    closing: bool = False,
) -> RiskVerdict:
    """`order_notional_usd` is the dollar value of the order about to be
    placed (qty * price), not a base-unit quantity: that is what makes
    every limit below mean the same thing whether the asset is a coin or
    a stock. Position value is derived from the snapshot's own inventory
    and mid, never trusted from anywhere else.

    `closing=True` is for an order that only reduces the position. The
    kill limits still apply; the order-size and inventory-age vetoes do
    not, because blocking the exit is what froze the loop with a stuck
    position in the first published version."""
    # 1. max drawdown
    if snapshot["drawdown_pct"] > limits.max_drawdown_pct:
        return RiskVerdict(False, "max_drawdown breached", kill=True)

    # 2. max position (in dollars)
    position_usd = abs(snapshot["inventory"]) * snapshot["mid"]
    if position_usd > limits.max_position_usd:
        return RiskVerdict(False, "max_position_usd breached", kill=True)

    # 3. max daily loss
    if snapshot["daily_loss_usd"] > limits.max_daily_loss_usd:
        return RiskVerdict(False, "max_daily_loss breached", kill=True)

    # 4. max order notional
    if not closing and order_notional_usd > limits.max_order_notional_usd:
        return RiskVerdict(False, "order exceeds max_order_notional_usd")

    # 5. max inventory age
    if (
        not closing
        and snapshot["inventory"] != 0
        and snapshot["position_age_s"] > limits.max_inventory_age_s
    ):
        return RiskVerdict(False, "inventory held past max_inventory_age_s")

    # 6. max stale-data age
    if snapshot["data_age_s"] > limits.max_stale_data_age_s:
        return RiskVerdict(False, "market data stale past max_stale_data_age_s")

    # 7. max API errors
    if api_error_streak > limits.max_api_errors:
        return RiskVerdict(False, "max_api_errors breached", kill=True)

    # 8. max decision latency
    if (
        decision_latency_ms is not None
        and decision_latency_ms > limits.max_decision_latency_ms
    ):
        return RiskVerdict(False, "decision latency over max_decision_latency_ms")

    # 9. max leverage (spot only; always 1.0, checked anyway so the limit is real)
    if snapshot.get("leverage", 1.0) > limits.max_leverage:
        return RiskVerdict(False, "max_leverage breached", kill=True)

    return RiskVerdict(True)
