from jevloop.limits import Limits
from jevloop.risk import check

L = Limits()

OK_SNAPSHOT = dict(
    drawdown_pct=0.01,
    inventory=0.0005,
    mid=40_000.0,  # position value = 0.0005 * 40,000 = $20, under max_position_usd (50.0)
    daily_loss_usd=1.0,
    position_age_s=10.0,
    data_age_s=0.2,
    leverage=1.0,
)


def test_ok_case_passes():
    v = check(
        OK_SNAPSHOT,
        order_notional_usd=20.0,
        limits=L,
        api_error_streak=0,
        decision_latency_ms=90.0,
    )
    assert v.ok
    assert v.veto is None
    assert not v.kill


def test_drawdown_kills():
    snap = dict(OK_SNAPSHOT, drawdown_pct=0.5)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


def test_max_position_kills():
    # inventory * mid must exceed max_position_usd (50.0 by default)
    snap = dict(OK_SNAPSHOT, inventory=1.0, mid=85_000.0)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


def test_max_position_scales_with_price_not_just_quantity():
    # Same tiny quantity, but a high enough price still breaches the dollar cap.
    snap = dict(OK_SNAPSHOT, inventory=0.01, mid=10_000.0)  # $100 position
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


def test_max_daily_loss_kills():
    snap = dict(OK_SNAPSHOT, daily_loss_usd=999.0)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


def test_order_notional_vetoes_without_kill():
    v = check(
        OK_SNAPSHOT,
        order_notional_usd=10_000.0,
        limits=L,
        api_error_streak=0,
        decision_latency_ms=90.0,
    )
    assert not v.ok and not v.kill


def test_inventory_age_vetoes_without_kill():
    snap = dict(OK_SNAPSHOT, position_age_s=99999.0)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and not v.kill


def test_stale_data_vetoes():
    snap = dict(OK_SNAPSHOT, data_age_s=999.0)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and not v.kill


def test_api_error_streak_kills():
    v = check(OK_SNAPSHOT, 20.0, L, api_error_streak=99, decision_latency_ms=90.0)
    assert not v.ok and v.kill


def test_decision_latency_vetoes_without_kill():
    v = check(OK_SNAPSHOT, 20.0, L, api_error_streak=0, decision_latency_ms=99999.0)
    assert not v.ok and not v.kill


def test_leverage_kills():
    snap = dict(OK_SNAPSHOT, leverage=5.0)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


def test_risk_never_reads_jev_answers():
    """Structural guarantee: check() takes a snapshot, not a battery answer dict."""
    import inspect

    sig = inspect.signature(check)
    assert "answers" not in sig.parameters
