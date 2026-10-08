"""The loop must stay under the Vercel AI Gateway's ~20-30 requests a minute."""
from unittest import mock

import pytest

from jevloop import client as jc
from jevloop.client import JevThrottle, RateLimited

SNAP = {"mid": 100.0, "spread_bps": 5.0, "inventory": 0.0}
ANS = {"regime": {"choice": "trending"}}


def _throttle():
    return JevThrottle(min_interval_s=4.0, min_gap_s=3.0, move_bps=15.0, max_age_s=60.0)


def test_calls_at_most_once_per_interval_on_a_500ms_loop():
    t = _throttle()
    calls = 0
    for i in range(120):  # one minute of 500 ms ticks, state unchanged
        now = i * 0.5
        if t.due(SNAP, now):
            t.called(now)
            t.success(ANS, {"latency_ms": 90}, SNAP, now)
            calls += 1
    assert calls == 15  # one every 4 s


def test_material_change_calls_early_but_never_under_the_gap():
    t = _throttle()
    t.called(0.0)
    t.success(ANS, {}, SNAP, 0.0)
    moved = dict(SNAP, mid=101.0)  # 100 bps
    assert t.due(moved, 1.0) is None  # under min_gap_s
    assert t.due(moved, 3.0) == "price moved"
    assert t.due(SNAP, 3.0) is None  # no change, interval not up


def test_rate_limit_backs_off_exponentially_and_keeps_the_last_judgment():
    t = _throttle()
    t.called(0.0)
    t.success(ANS, {"latency_ms": 90}, SNAP, 0.0)
    w1 = t.rate_limited(4.0)
    assert t.due(SNAP, 4.0 + w1 - 0.01) is None
    w2 = t.rate_limited(4.0 + w1)
    assert w2 > w1
    answers, meta = t.last(10.0)
    assert answers == ANS and meta["reused"] is True


def test_old_judgment_is_not_reused():
    t = _throttle()
    t.success(ANS, {}, SNAP, 0.0)
    assert t.last(61.0) is None


@pytest.mark.parametrize("status,text", [(429, ""), (400, "access frequency is too high")])
def test_rate_limit_responses_raise_without_retrying(status, text):
    resp = mock.Mock(status_code=status, text=text)
    with mock.patch.object(jc.requests, "post", return_value=resp) as post:
        with pytest.raises(RateLimited):
            jc._post_with_retry("https://x", {}, {}, timeout=2.0)
    assert post.call_count == 1  # retrying a rate limit only adds requests
