import pytest

from jevloop.loop import _StopRequested, _handle_sigterm, main


def test_sigterm_handler_raises_stop_requested():
    with pytest.raises(_StopRequested):
        _handle_sigterm(signum=15, frame=None)


def test_ticks_zero_and_forever_both_mean_run_forever(monkeypatch):
    # main() maps --ticks 0 and --forever to ticks=None (run forever) before
    # ever calling run(); intercept run() itself so no network/loop executes.
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("jevloop.loop.run", fake_run)

    main(["--ticks", "0", "--symbol", "BTC/USD"])
    assert calls[-1]["ticks"] is None

    main(["--forever", "--symbol", "BTC/USD"])
    assert calls[-1]["ticks"] is None


def test_explicit_ticks_count_is_preserved(monkeypatch):
    calls = []
    monkeypatch.setattr("jevloop.loop.run", lambda **kwargs: calls.append(kwargs) or 0)
    main(["--ticks", "30", "--symbol", "BTC/USD"])
    assert calls[-1]["ticks"] == 30
    assert calls[-1]["live"] is False
