"""--bar: one decision per bar close (e.g. 30-minute bars) instead of every 2 s block."""

from jevloop import loop
from jevloop.limits import Limits


def test_bar_close_is_utc_aligned_with_a_short_settle():
    base = 1800.0 * 1_000_000  # a :00 or :30 boundary
    assert loop._seconds_to_bar_close(base + 100, 1800) == 1800 - 100 + loop.BAR_SETTLE_S
    assert loop._seconds_to_bar_close(base + 1, 1800) == 1.0  # still inside the settle
    assert loop._seconds_to_bar_close(base + loop.BAR_SETTLE_S, 1800) == 1800


def test_cli_bar_30m_passes_1800_seconds_and_default_is_off(monkeypatch):
    calls = []
    monkeypatch.setattr("jevloop.loop.run", lambda **kw: calls.append(kw) or 0)
    loop.main(["--bar", "30m", "--symbol", "PAXG/USD", "--ticks", "3"])
    assert calls[-1]["bar_seconds"] == 1800.0 and calls[-1]["symbol"] == "PAXG/USD"
    loop.main(["--ticks", "3"])
    assert calls[-1]["bar_seconds"] is None


class _FakeAlpaca:
    def __init__(self):
        self.bar_fetches = 0

    def get_account(self):
        return {"equity": "1000"}

    def get_orderbook(self):
        return {"b": [{"p": 4250.0, "s": 1.0}], "a": [{"p": 4260.0, "s": 1.0}]}

    def get_recent_trades(self, start_iso):
        return []

    def get_minute_bars(self, start_iso):
        self.bar_fetches += 1
        import time as _t
        from datetime import datetime, timezone

        now = _t.time()
        return [
            {"t": datetime.fromtimestamp(now - 60 * (40 - i) - 60, timezone.utc).isoformat(), "c": 4200.0 + i}
            for i in range(40)
        ]

    def cancel_own_orders(self):
        return []

    def get_own_orders(self, after_iso):
        return []

    def get_position_qty(self):
        return 0.0


def test_bar_mode_waits_for_each_close_and_refreshes_minute_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "LOG_DIR", tmp_path)
    monkeypatch.setattr(loop, "LOG_FILE", tmp_path / "log.jsonl")
    monkeypatch.setattr(loop, "LATEST_FILE", tmp_path / "latest.json")
    alp = _FakeAlpaca()
    monkeypatch.setattr(loop, "client_from_env", lambda **kw: alp)
    sleeps, closes = [], []
    monkeypatch.setattr(loop.time, "sleep", lambda s: sleeps.append(s))
    real_close = loop._seconds_to_bar_close
    monkeypatch.setattr(loop, "_seconds_to_bar_close", lambda *a: closes.append(real_close(*a)) or closes[-1])
    rc = loop.run(symbol="PAXG/USD", ticks=2, mock=True, limits=Limits(), bar_seconds=1800.0)
    assert rc == 0
    assert len(closes) == 2 and all(c in sleeps for c in closes)  # slept to each close
    assert all(s <= 1800 + loop.BAR_SETTLE_S for s in sleeps)  # no 2 s cadence on top
    assert alp.bar_fetches == 3  # startup backfill + one refresh per bar close
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
