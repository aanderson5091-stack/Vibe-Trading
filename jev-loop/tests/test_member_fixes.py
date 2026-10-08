"""Regression tests for the bugs members reported on the published prompt:
the Windows latest.json lock crash, fake price history, VWAP double
counting, a drawdown switch that could never fire, KILL not selling,
assumed fills, account-wide cancels, the calibration proxy, and mock
decisions placing orders."""

import json
import os
import time

import pytest

from jevloop import calibrate, loop, serve
from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import AlpacaAPIError, AlpacaPaperClient
from jevloop.state import InventoryState, apply_fill, build_snapshot, update_vwap

# -- 1. Windows: swapping latest.json while the dashboard reads it -----------


def test_atomic_write_retries_through_a_windows_style_lock(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    target.write_text('{"old": true}')
    real_replace = os.replace
    calls = {"n": 0}

    def locked_twice(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(loop.os, "replace", locked_twice)
    assert loop._atomic_write(target, '{"new": true}') is True
    assert calls["n"] == 3
    assert json.loads(target.read_text()) == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_skips_a_refresh_instead_of_crashing(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    target.write_text('{"old": true}')

    def always_locked(src, dst):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(loop.os, "replace", always_locked)
    assert loop._atomic_write(target, '{"new": true}', first_wait_s=0.001) is False
    assert json.loads(target.read_text()) == {"old": True}  # old copy intact
    assert not list(tmp_path.glob("*.tmp"))


def test_reader_tolerates_lock_and_partial_file(tmp_path, monkeypatch):
    feed = tmp_path / "latest.json"
    feed.write_text('{"ticks": [1]}')
    assert json.loads(serve.read_feed(feed)) == {"ticks": [1]}

    feed.write_text('{"ticks": [1, 2')  # caught mid-write
    assert json.loads(serve.read_feed(feed, wait_s=0.001)) == {"ticks": [1]}

    real = type(feed).read_bytes
    calls = {"n": 0}

    def locked_once(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "locked")
        return real(self)

    feed.write_text('{"ticks": [1, 2]}')
    monkeypatch.setattr(type(feed), "read_bytes", locked_once)
    assert json.loads(serve.read_feed(feed, wait_s=0.001)) == {"ticks": [1, 2]}


# -- 2. real price history, real timestamps, VWAP once -----------------------


def _snap(prices, inv=None, now=None, mid=100.0):
    now = now or time.time()
    return build_snapshot(
        as_of=now,
        mid=mid,
        microprice=mid,
        spread_bps=1.0,
        bid_depth=[(99.9, 1)],
        ask_depth=[(100.1, 1)],
        trade_prices=prices,
        trade_sides=[(now - 1, "buy")],
        inv=inv or InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0),
        data_timestamp=now,
    )


def test_moving_prices_give_nonzero_returns_and_vol():
    now = time.time()
    # 31 one-minute points that actually move
    prices = [(now - 60 * k, 100 + (k % 3) - 0.05 * k) for k in range(31, 0, -1)]
    prices.append((now, 101.0))
    snap = _snap(prices, now=now)
    for key in (
        "return_1m",
        "return_5m",
        "return_30m",
        "realised_vol_short",
        "realised_vol_medium",
    ):
        assert snap[key] not in (None, 0.0), key


def test_flat_history_is_zero_and_missing_history_is_none():
    now = time.time()
    assert _snap([(now, 100.0)], now=now)["return_1m"] is None
    assert _snap([(now, 100.0)], now=now)["realised_vol_short"] is None


def test_parse_ts_keeps_real_venue_time():
    ts = loop._parse_ts("2026-09-24T21:44:30.980346366Z")
    assert abs(ts - 1790286270.980346) < 1e-3


def test_vwap_counts_each_trade_once_across_overlapping_fetches():
    inv = InventoryState()
    batch = [(1.0, 100.0, 1.0), (2.0, 102.0, 1.0)]
    update_vwap(inv, batch)
    update_vwap(inv, batch)  # same trades seen again next tick
    update_vwap(inv, batch + [(3.0, 110.0, 2.0)])
    assert inv.vwap_cum_vol == 4.0
    assert inv.vwap_cum_pv == 100 + 102 + 220


# -- 3. drawdown switch can fire ---------------------------------------------


def test_drawdown_is_measured_against_real_equity():
    inv = InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0)
    apply_fill(inv, "buy", 1.0, 1000.0, time.time())
    snap = _snap([(time.time(), 900.0)], inv=inv, mid=900.0)
    assert snap["unrealised_pnl_usd"] == -100.0
    assert snap["drawdown_pct"] == pytest.approx(0.10)


def test_apply_fill_realises_pnl():
    inv = InventoryState()
    apply_fill(inv, "buy", 2.0, 100.0, 0)
    apply_fill(inv, "sell", 1.0, 110.0, 1)
    assert inv.inventory == 1.0 and inv.realised_pnl_usd == 10.0
    apply_fill(inv, "sell", 1.0, 90.0, 2)
    assert inv.inventory == 0.0 and inv.realised_pnl_usd == 0.0


# -- 4-6. fills from the broker, own orders only, KILL flattens -------------


class FakeAlpaca:
    """Stands in for AlpacaPaperClient's order endpoints."""

    order_prefix = "jevloop-test-"

    def __init__(self, held=0.0):
        self._order_seq = 0
        self.orders = []
        self.cancelled = []
        self.held = held
        self.foreign_order = {
            "id": "other",
            "client_order_id": "someone-else",
            "status": "new",
        }

    def submit_market_order(self, side, qty):
        self._order_seq += 1
        o = {
            "id": f"o{self._order_seq}",
            "client_order_id": f"{self.order_prefix}{self._order_seq}",
            "side": side,
            "status": "filled",
            "filled_qty": str(qty),
            "filled_avg_price": "100.5",
        }
        self.orders.append(o)
        self.held += qty if side == "buy" else -qty
        return o

    def get_own_orders(self, after_iso):
        return [
            o for o in self.orders if o["client_order_id"].startswith(self.order_prefix)
        ]

    def cancel_own_orders(self):
        mine = [o for o in self.orders if o["status"] == "new"]
        self.cancelled += [o["id"] for o in mine]
        return len(mine)

    def get_position_qty(self):
        return self.held


def test_fills_are_read_back_not_assumed():
    alp = FakeAlpaca()
    inv = InventoryState()
    alp.submit_market_order("buy", 0.5)
    assert inv.inventory == 0.0  # nothing assumed on submit
    seen = {}
    fills, pending = loop.reconcile_fills(alp, inv, seen, {}, "x", time.time())
    assert fills == [("buy", 0.5, 100.5)] and inv.inventory == 0.5 and not pending
    fills, _ = loop.reconcile_fills(alp, inv, seen, {}, "x", time.time())
    assert fills == [] and inv.inventory == 0.5  # never counted twice


def test_kill_sends_a_real_closing_order_and_never_sells_more_than_held():
    alp = FakeAlpaca()
    inv = InventoryState()
    alp.submit_market_order("buy", 0.5)
    seen = {}
    loop.reconcile_fills(alp, inv, seen, {}, "x", time.time())
    alp.held = 0.3  # the account holds less than the bot thinks
    txt = loop._flatten(
        alp, resolve_symbol("BTC/USD"), inv, False, seen, {}, "x", time.time()
    )
    closing = alp.orders[-1]
    assert closing["side"] == "sell" and float(closing["filled_qty"]) == 0.3
    assert "market sell 0.3" in txt


def test_kill_is_dry_in_dry_mode():
    alp = FakeAlpaca()
    txt = loop._flatten(
        alp,
        resolve_symbol("BTC/USD"),
        InventoryState(inventory=1.0),
        True,
        {},
        {},
        "x",
        0,
    )
    assert txt.startswith("dry:") and alp.orders == []


def test_cancel_never_hits_the_account_wide_endpoint(monkeypatch):
    client = AlpacaPaperClient(
        api_key="x", secret_key="y", spec=resolve_symbol("BTC/USD")
    )
    assert not hasattr(client, "cancel_all_orders")
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url))
        if method == "GET":
            return [
                {"id": "a", "client_order_id": client.order_prefix + "1"},
                {"id": "b", "client_order_id": "manual-order"},
            ]
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.cancel_own_orders() == 1
    deletes = [u for m, u in calls if m == "DELETE"]
    assert deletes == [f"{client.base_url}/v2/orders/a"]


def test_orders_carry_this_runs_client_order_id(monkeypatch):
    client = AlpacaPaperClient(
        api_key="x", secret_key="y", spec=resolve_symbol("BTC/USD")
    )
    bodies = []
    monkeypatch.setattr(
        client, "_request", lambda m, u, **kw: bodies.append(kw["json"]) or kw["json"]
    )
    client.submit_limit_order("buy", 0.001, 100.0)
    client.submit_market_order("sell", 0.001)
    assert all(b["client_order_id"].startswith(client.order_prefix) for b in bodies)


# -- 7. calibration uses the direction confidence ---------------------------


def test_calibration_scores_direction_confidence_not_quote_env():
    ticks = [
        {
            "direction": "up",
            "direction_conf": 0.9,
            "quote_environment_conf": 0.1,
            "mid": 100,
        },
        {
            "direction": "down",
            "direction_conf": 0.6,
            "quote_environment_conf": 0.9,
            "mid": 101,
        },
        {"direction": "neutral", "direction_conf": 0.8, "mid": 102},
        {"direction": None, "mid": 99},
    ]
    pairs = calibrate.pair_predictions(ticks, horizon=1)
    # up at 100 -> 101: right. down at 101 -> 102: wrong. neutral: not scored.
    assert pairs == [(0.9, 1), (0.6, 0)]


def test_calibration_skips_old_logs_without_direction_conf():
    ticks = [
        {"direction": "up", "quote_environment_conf": 0.9, "mid": 100},
        {"mid": 101},
    ]
    assert calibrate.pair_predictions(ticks, horizon=1) == []


# -- Alpaca crypto is spot, long only: SELL closes the long, never shorts ----


def _sell_signal(monkeypatch, alp, inv, dry=False):
    from jevloop import risk
    from jevloop.limits import Limits
    from jevloop.policy import QUOTE_BOTH_SIDES, Action

    monkeypatch.setattr(risk, "check", lambda *a, **k: risk.RiskVerdict(ok=True))
    return loop._execute_action(
        alpaca=alp,
        spec=resolve_symbol("BTC/USD"),
        action=Action(QUOTE_BOTH_SIDES, "test", skew=-0.5, direction_leg="down"),
        bid_px=100.0,
        ask_px=101.0,
        mid=100.5,
        quote_notional=10.0,
        directional_notional=10.0,
        snapshot={},
        limits=Limits(),
        inv=inv,
        api_error_streak=0,
        decision_latency_ms=100.0,
        resting_quotes={"bid": 100.0, "ask": 101.0},  # no re-quote this tick
        rest_counter=0,
        now=time.time(),
        dry=dry,
        expected_px={},
    )


def test_sell_when_flat_does_nothing(monkeypatch):
    alp = FakeAlpaca()
    for dry in (False, True):
        _, fill_txt, *_ = _sell_signal(monkeypatch, alp, InventoryState(), dry=dry)
        assert "no position to close" in fill_txt
    assert alp.orders == []  # no order, so no short


def test_sell_when_long_closes_the_long_and_no_more(monkeypatch):
    alp = FakeAlpaca()
    inv = InventoryState()
    alp.submit_market_order("buy", 0.0005)
    loop.reconcile_fills(alp, inv, {}, {}, "x", time.time())
    alp.held = 0.0003  # the account holds less than this run thinks
    _, fill_txt, *_ = _sell_signal(monkeypatch, alp, inv)
    closing = alp.orders[-1]
    assert closing["side"] == "sell" and float(closing["filled_qty"]) == 0.0003
    assert "closing long" in fill_txt and alp.held == 0.0  # flat, never short
