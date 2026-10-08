"""Avellaneda-Stoikov reservation price and half spread, in basis points.

Fifty-year-old market-making maths. Stays in code. No model call, ever.
Jev never sees this file's output as a question. It only answers whether
the state is worth quoting into at all (policy.py). This file answers
where to quote.

Everything is worked out in basis points of mid, never in dollars, so the
same gamma and kappa give a sensible spread on an $85,000 coin and a $30
stock alike. (The first published version added the liquidity term in
dollars: about $1.29 each side, which is 0.3 bps on BTC and 860 bps on a
$30 stock.)

    q             = position value / max_position_usd   (-1 .. 1)
    reservation   = -q * gamma * sigma_bps^2 * T         (bps offset from mid)
    half spread   = gamma * sigma_bps^2 * T + (2 / gamma) * ln(1 + gamma / kappa)

sigma_bps is the per-minute realised volatility in bps, T the horizon in
minutes. The half spread is clamped to [min_half_bps, max_half_bps].
"""

from __future__ import annotations

import math


def reservation_offset_bps(
    inventory_frac: float, gamma: float, sigma_bps: float, horizon_min: float
) -> float:
    """Skew away from the side that grows inventory: long (q > 0) pulls the
    reservation price below mid so the quotes favour selling."""
    return -inventory_frac * gamma * (sigma_bps**2) * horizon_min


def half_spread_bps(
    gamma: float, sigma_bps: float, horizon_min: float, kappa: float
) -> float:
    """Half the total quoted spread around the reservation price, in bps."""
    inventory_term = gamma * (sigma_bps**2) * horizon_min
    liquidity_term = (2.0 / gamma) * math.log1p(gamma / kappa)
    return inventory_term + liquidity_term


def quote_prices(
    mid: float,
    inventory_frac: float,
    sigma: float,
    gamma: float,
    kappa: float,
    horizon_s: float,
    min_half_bps: float = 2.0,
    max_half_bps: float = 50.0,
) -> tuple[float, float]:
    """Returns (bid, ask). `sigma` is the per-minute realised volatility as
    a fraction (0.0005 = 5 bps), as state.py computes it."""
    sigma_bps = sigma * 10_000
    horizon_min = horizon_s / 60.0
    q = max(-1.0, min(1.0, inventory_frac))
    r = reservation_offset_bps(q, gamma, sigma_bps, horizon_min)
    h = half_spread_bps(gamma, sigma_bps, horizon_min, kappa)
    h = max(min_half_bps, min(max_half_bps, h))
    return mid * (1 + (r - h) / 10_000), mid * (1 + (r + h) / 10_000)
