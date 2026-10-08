import time

from jevloop.state import InventoryState, approx_token_count, build_snapshot


def _snapshot(now):
    inv = InventoryState(
        inventory=0.001,
        entry_price=99.5,
        position_opened_at=now - 30,
        equity_usd=1000.0,
        high_water_mark_usd=1000.0,
        fills=3,
        orders_submitted=4,
        recent_latencies_ms=[80, 90, 75],
    )
    prices = [(now - i, 100 + (i % 5)) for i in range(0, 120)]
    sides = [(now - i, "buy" if i % 2 == 0 else "sell") for i in range(0, 40)]
    return build_snapshot(
        as_of=now,
        mid=100.2,
        microprice=100.21,
        spread_bps=3.4,
        bid_depth=[(100.1, 0.5), (100.0, 0.4), (99.9, 0.3)],
        ask_depth=[(100.3, 0.4), (100.4, 0.3), (100.5, 0.2)],
        trade_prices=prices,
        trade_sides=sides,
        inv=inv,
        data_timestamp=now - 0.5,
    )


def test_snapshot_has_all_required_field_groups():
    now = time.time()
    snap = _snapshot(now)
    for key in [
        "mid",
        "microprice",
        "return_1m",
        "return_5m",
        "return_30m",
        "spread_bps",
        "bid_depth_3",
        "ask_depth_3",
        "imbalance",
        "aggressive_buy_ratio",
        "trade_intensity_per_s",
        "realised_vol_short",
        "realised_vol_medium",
        "inventory",
        "unrealised_pnl_usd",
        "daily_loss_usd",
        "drawdown_pct",
        "position_age_s",
        "fill_ratio",
        "reject_count",
        "last_10_latencies_ms",
        "data_age_s",
    ]:
        assert key in snap, f"missing field {key}"


def test_snapshot_stays_under_roughly_400_tokens():
    now = time.time()
    snap = _snapshot(now)
    assert approx_token_count(snap) < 400


def test_timestamp_discipline_ignores_future_data():
    now = time.time()
    inv = InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0)
    # A price point stamped in the future must never affect a return calc.
    prices = [(now - 70, 100.0), (now - 10, 105.0), (now + 1000, 999999.0)]
    snap = build_snapshot(
        as_of=now,
        mid=100.0,
        microprice=100.0,
        spread_bps=3.0,
        bid_depth=[(99.9, 1)],
        ask_depth=[(100.1, 1)],
        trade_prices=prices,
        trade_sides=[(now - 10, "buy")],
        inv=inv,
        data_timestamp=now,
    )
    assert snap["return_1m"] is not None
    assert snap["return_1m"] < 100


def test_top3_depth_used_for_imbalance():
    now = time.time()
    inv = InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0)
    snap = build_snapshot(
        as_of=now,
        mid=100.0,
        microprice=100.0,
        spread_bps=3.0,
        bid_depth=[(99.9, 10), (99.8, 10), (99.7, 10)],
        ask_depth=[(100.1, 1), (100.2, 1), (100.3, 1)],
        trade_prices=[(now, 100.0)],
        trade_sides=[(now, "buy")],
        inv=inv,
        data_timestamp=now,
    )
    assert snap["imbalance"] > 0.5  # much more bid depth than ask depth
