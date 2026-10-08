"""Start / Stop from the dashboard: a pause blocks every new order, Start
resumes, and the control endpoint only takes a POST carrying this install's
token, addressed to 127.0.0.1, with no CORS."""

import http.client
import json
import threading
import time

import pytest

from jevloop import control, loop, serve
from jevloop.assets import resolve_symbol
from jevloop.limits import Limits
from jevloop.policy import QUOTE_BOTH_SIDES, Action
from jevloop.state import InventoryState


class _Broker:
    _order_seq = 0

    def __init__(self, held=0.0):
        self.sent, self.cancels, self.held = [], 0, held

    def submit_limit_order(self, side, qty, px):
        self.sent.append(("limit", side, qty))
        return {"client_order_id": f"t-{len(self.sent)}"}

    def submit_market_order(self, side, qty):
        self.sent.append(("market", side, qty))
        return {"client_order_id": f"t-{len(self.sent)}"}

    def cancel_own_orders(self):
        self.cancels += 1
        return 0

    def get_position_qty(self):
        return self.held


def _tick(monkeypatch, broker, leg, inv=None, resting=None):
    from jevloop import risk

    monkeypatch.setattr(risk, "check", lambda *a, **k: risk.RiskVerdict(ok=True))
    return loop._execute_action(
        alpaca=broker,
        spec=resolve_symbol("BTC/USD"),
        action=Action(QUOTE_BOTH_SIDES, "test", skew=0.5, direction_leg=leg),
        bid_px=100.0,
        ask_px=101.0,
        mid=100.5,
        quote_notional=20.0,
        directional_notional=20.0,
        snapshot={},
        limits=Limits(),
        inv=inv or InventoryState(),
        api_error_streak=0,
        decision_latency_ms=100.0,
        resting_quotes=resting,
        rest_counter=0,
        now=time.time(),
        dry=False,
        expected_px={},
    )


def test_no_control_file_means_running(_isolated_jev_home):
    assert control.read_state(_isolated_jev_home) == control.RUNNING


def test_pause_blocks_every_new_order_and_pulls_resting_quotes(monkeypatch, _isolated_jev_home):
    control.write_state(_isolated_jev_home, control.PAUSED)
    inv = InventoryState(inventory=0.5)
    for leg in ("up", "down", None):
        b = _Broker(held=0.5)
        line, fill, *_rest = _tick(monkeypatch, b, leg, inv=inv, resting={"bid": 1, "ask": 2})
        assert b.sent == []  # no quote, no buy leg, no sell leg
        assert line.startswith("PAUSED") and b.cancels == 1  # resting quotes pulled
        assert _rest[-2] is None  # nothing left marked as resting
    assert inv.inventory == 0.5  # the position is untouched: Stop is not KILL


def test_resume_places_orders_again(monkeypatch, _isolated_jev_home):
    control.write_state(_isolated_jev_home, control.PAUSED)
    b = _Broker()
    _tick(monkeypatch, b, "up")
    assert b.sent == []
    control.write_state(_isolated_jev_home, control.RUNNING)
    line, *_ = _tick(monkeypatch, b, "up")
    assert not line.startswith("PAUSED")
    assert ("limit", "buy", pytest.approx(b.sent[0][2])) in b.sent and ("market", "buy", pytest.approx(b.sent[-1][2])) in b.sent


def test_pause_survives_and_a_bad_file_fails_safe(_isolated_jev_home):
    control.write_state(_isolated_jev_home, control.PAUSED)
    assert control.read_state(_isolated_jev_home) == control.PAUSED  # a fresh read, like a refresh
    (_isolated_jev_home / "control.json").write_text("{half a fi")
    assert control.is_paused(_isolated_jev_home)  # unreadable: don't trade


def test_cli_pause_and_resume(monkeypatch, _isolated_jev_home):
    from jevloop import __main__ as cli

    monkeypatch.setattr("sys.argv", ["jevloop", "pause"])
    assert cli.main() == 0 and control.is_paused(_isolated_jev_home)
    monkeypatch.setattr("sys.argv", ["jevloop", "resume"])
    assert cli.main() == 0 and not control.is_paused(_isolated_jev_home)


def test_token_is_per_install_and_stable(_isolated_jev_home, tmp_path):
    t1 = control.load_or_create_token(_isolated_jev_home)
    assert len(t1) >= 32 and control.load_or_create_token(_isolated_jev_home) == t1
    assert control.load_or_create_token(tmp_path / "other") != t1


# -- the HTTP endpoint -------------------------------------------------------


@pytest.fixture
def server(_isolated_jev_home):
    import socketserver

    httpd = socketserver.TCPServer(("127.0.0.1", 0), serve.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], control.load_or_create_token(_isolated_jev_home)
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}"}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


def test_post_needs_the_token(server, _isolated_jev_home):
    port, token = server
    for hdrs in ({}, {"X-Jev-Token": "wrong"}, {"X-Jev-Token": token[:-1]}):
        status, _, _ = _req(port, "POST", "/control", {"state": "PAUSED"}, hdrs)
        assert status == 403
    assert not control.is_paused(_isolated_jev_home)
    status, h, body = _req(port, "POST", "/control", {"state": "PAUSED"}, {"X-Jev-Token": token})
    assert status == 200 and json.loads(body) == {"state": "PAUSED"}
    assert control.is_paused(_isolated_jev_home)
    assert not any(k.lower().startswith("access-control") for k in h)  # no CORS
    status, _, body = _req(port, "GET", "/control")
    assert status == 200 and json.loads(body)["state"] == "PAUSED"
    status, _, _ = _req(port, "POST", "/control", {"state": "RUNNING"}, {"X-Jev-Token": token})
    assert status == 200 and not control.is_paused(_isolated_jev_home)


def test_other_sites_are_refused(server, _isolated_jev_home):
    port, token = server
    good = {"X-Jev-Token": token}
    # a page on another site, even with the token somehow in hand
    assert _req(port, "POST", "/control", {"state": "PAUSED"}, {**good, "Origin": "https://evil.example"})[0] == 403
    # DNS rebinding: another name pointed at 127.0.0.1
    assert _req(port, "POST", "/control", {"state": "PAUSED"}, {**good, "Host": f"evil.example:{port}"})[0] == 403
    assert _req(port, "GET", "/", headers={"Host": f"evil.example:{port}"})[0] == 403
    # no CORS preflight is answered, and GET can't change anything
    status, h, _ = _req(port, "OPTIONS", "/control", headers={"Origin": "https://evil.example"})
    assert status >= 400 and not any(k.lower().startswith("access-control") for k in h)
    assert _req(port, "GET", "/control?state=PAUSED")[0] == 200
    assert _req(port, "POST", "/control", {"state": "KILL"}, good)[0] == 400
    assert not control.is_paused(_isolated_jev_home)


def test_dashboard_page_carries_the_token_and_the_buttons(server):
    port, token = server
    status, _, body = _req(port, "GET", "/")
    page = body.decode()
    assert status == 200 and token in page and "__JEV_CONTROL_TOKEN__" not in page
    assert 'id="ctlstart"' in page and 'id="ctlstop"' in page and "KILL" in page


def test_server_binds_loopback_only():
    import inspect

    src = inspect.getsource(serve.main)
    assert 'TCPServer(("127.0.0.1"' in src and "0.0.0.0" not in src
