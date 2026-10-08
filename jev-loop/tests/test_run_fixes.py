"""Regression tests for the second round of member-reported bugs: buys
piling up past the position cap, a spread fixed in dollars, a stuck
position freezing the loop, orders left open after a run, fees not
tracked, closing sells rounded up, and a network timeout crashing the
loop."""

import time

import pytest
import requests

from jevloop import loop
from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import AlpacaAPIError, AlpacaPaperClient
from jevloop.limits import Limits
from jevloop.policy import QUOTE_BOTH_SIDES, Action
from jevloop.pricing import quote_prices
from jevloop.risk import check
from jevloop.state import InventoryState, apply_fill


class _Broker:
    order_prefix = "jevloop-test-"

    def __init__(self, held=0.0):
        self._order_seq = 0
        self.sent, self.cancels, self.held = [], 0, held

    def submit_limit_order(self, side, qty, px):
        self._order_seq += 1
        self.sent.append(("limit", side, qty))
        return {"client_order_id": f"t-{self._order_seq}"}

    def submit_market_order(self, side, qty):
        self._order_seq += 1
        self.sent.append(("market", side, qty))
        return {"client_order_id": f"t-{self._order_seq}"}

    def cancel_own_orders(self):
        self.cancels += 1
        return 0

    def get_position_qty(self):
        return self.held


def _snap(**kw):
    base = dict(
        drawdown_pct=0.0, inventory=0.0, mid=100.0, daily_loss_usd=0.0,
        position_age_s=0.0, data_age_s=0.1,
    )
    base.update(kw)
    return base


def _execute(broker, inv, snapshot, *, resting=None, leg=None, pending=0.0, mid=100.0):
    return loop._execute_action(
        alpaca=broker,
        spec=resolve_symbol("ETH/USD"),
        action=Action(QUOTE_BOTH_SIDES, "test", skew=0.0, direction_leg=leg),
        bid_px=mid * 0.999,
        ask_px=mid * 1.001,
        mid=mid,
        quote_notional=20.0,
        directional_notional=20.0,
        snapshot=snapshot,
        limits=Limits(),
        inv=inv,
        api_error_streak=0,
        decision_latency_ms=100.0,
        resting_quotes=resting,
        rest_counter=0,
        now=time.time(),
        expected_px={},
        pending_buy_usd=pending,
    )


# -- 1. buys counted against the $50 cap before they fill -------------------


def test_bid_and_buy_leg_stop_at_the_cap():
    b = _Broker(held=0.3)  # $30 held at $100, $20 of room
    inv = InventoryState(inventory=0.3)
    _, fill_txt, *_ = _execute(b, inv, _snap(inventory=0.3), leg="up")
    buys = [s for s in b.sent if s[1] == "buy"]
    assert len(buys) == 1  # the $20 bid fits, the $20 leg on top does not
    assert "buy leg skipped" in fill_txt


def test_working_buys_block_a_new_buy_leg():
    b = _Broker()
    inv = InventoryState()
    # a $40 bid still working at the broker, no rest-cycle this tick
    _, fill_txt, *_ = _execute(b, inv, _snap(), resting={"bid": 1, "ask": 2}, leg="up", pending=40.0)
    assert not [s for s in b.sent if s[1] == "buy"]
    assert "buy leg skipped" in fill_txt


def test_reconcile_reports_working_buy_dollars():
    class A:
        def get_own_orders(self, after):
            return [
                {"id": "1", "client_order_id": "c1", "side": "buy", "status": "new",
                 "qty": "0.2", "filled_qty": "0", "limit_price": "100"},
                {"id": "2", "client_order_id": "c2", "side": "sell", "status": "new",
                 "qty": "0.2", "filled_qty": "0", "limit_price": "101"},
            ]
    ob = {}
    _, pending = loop.reconcile_fills(A(), InventoryState(), {}, {}, "x", 0, open_buys=ob)
    assert pending and ob["usd"] == pytest.approx(20.0)


# -- 2. spread in bps, the same on BTC and a $30 stock ----------------------


def test_spread_is_proportional_to_price():
    L = Limits()
    kw = dict(inventory_frac=0.0, sigma=0.0005, gamma=L.as_gamma, kappa=L.as_kappa,
              horizon_s=L.as_horizon_s)
    b1, a1 = quote_prices(mid=85_000.0, **kw)
    b2, a2 = quote_prices(mid=30.0, **kw)
    bps1 = (a1 - b1) / 85_000.0 * 1e4
    bps2 = (a2 - b2) / 30.0 * 1e4
    assert bps1 == pytest.approx(bps2)
    assert 2 <= bps1 <= 100


def test_long_inventory_skews_quotes_down():
    L = Limits()
    kw = dict(mid=100.0, sigma=0.001, gamma=L.as_gamma, kappa=L.as_kappa, horizon_s=60.0)
    b0, a0 = quote_prices(inventory_frac=0.0, **kw)
    b1, a1 = quote_prices(inventory_frac=1.0, **kw)
    assert b1 < b0 and a1 < a0


# -- 3. a stuck position is closed, not frozen ------------------------------


def test_aged_inventory_closes_instead_of_freezing():
    b = _Broker(held=0.3)
    inv = InventoryState(inventory=0.3)
    line, fill_txt, *_ = _execute(b, inv, _snap(inventory=0.3, position_age_s=99999.0))
    assert line.startswith("EXIT")
    assert b.sent == [("market", "sell", 0.3)]


def test_closing_orders_pass_the_age_veto_but_not_the_kill_limits():
    L = Limits()
    assert check(_snap(inventory=0.3, position_age_s=99999.0), 30.0, L, 0, 90.0, closing=True).ok
    v = check(_snap(inventory=0.3, drawdown_pct=0.5), 30.0, L, 0, 90.0, closing=True)
    assert not v.ok and v.kill


# -- 4. every way out cancels this run's resting orders ---------------------


def test_cancel_runs_on_every_way_out():
    import inspect

    src = inspect.getsource(loop.run)
    finally_block = src.rsplit("finally:", 1)[1]
    assert "cancel_own_orders()" in finally_block
    handler = src.split("except (KeyboardInterrupt, _StopRequested):")[1].split("finally:")[0]
    assert "cancel_own_orders" not in handler  # not only on Ctrl+C any more


# -- 5. fees taken in coin are tracked --------------------------------------


def test_fee_in_coin_shrinks_inventory_and_books_the_cost():
    inv = InventoryState()
    apply_fill(inv, "buy", 0.001, 100_000.0, 0)
    fee = loop.sync_fees(inv, run_held=0.0009975, price=100_000.0)
    assert inv.inventory == pytest.approx(0.0009975)
    assert fee == pytest.approx(0.25) and inv.fees_usd == pytest.approx(0.25)
    assert inv.realised_pnl_usd == pytest.approx(-0.25)


def test_no_fee_when_broker_matches():
    inv = InventoryState()
    apply_fill(inv, "buy", 0.001, 100_000.0, 0)
    assert loop.sync_fees(inv, run_held=0.001, price=100_000.0) == 0.0


# -- 6. closing sells round down --------------------------------------------


def test_closing_quantity_rounds_down():
    spec = resolve_symbol("BTC/USD")
    step = 10 ** -spec.qty_precision
    held = 3 * step - step / 3  # just under three steps
    assert loop._floor_qty(held, spec) <= held


# -- 7. a network timeout is an API error, not a crash ----------------------


def test_timeout_becomes_an_api_error(monkeypatch):
    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=resolve_symbol("BTC/USD"))

    def timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "request", timeout)
    with pytest.raises(AlpacaAPIError) as e:
        client.get_account()
    assert e.value.status_code == 0 and "network error" in str(e.value)
