from jevloop.ladder import Rung, select_rung


def test_kill_wins_over_everything():
    r = select_rung(
        risk_kill=True,
        decision_late=True,
        jev_down=True,
        decision_confidence=0.99,
        low_confidence_threshold=0.5,
    )
    assert r == Rung.KILL


def test_late_beats_jev_down_and_confidence():
    r = select_rung(
        risk_kill=False,
        decision_late=True,
        jev_down=True,
        decision_confidence=0.99,
        low_confidence_threshold=0.5,
    )
    assert r == Rung.HOLD_LATE


def test_jev_down_routes_to_rules_only():
    r = select_rung(
        risk_kill=False,
        decision_late=False,
        jev_down=True,
        decision_confidence=None,
        low_confidence_threshold=0.5,
    )
    assert r == Rung.RULES_ONLY


def test_low_confidence_reduces():
    r = select_rung(
        risk_kill=False,
        decision_late=False,
        jev_down=False,
        decision_confidence=0.2,
        low_confidence_threshold=0.5,
    )
    assert r == Rung.REDUCE


def test_healthy_high_confidence_runs():
    r = select_rung(
        risk_kill=False,
        decision_late=False,
        jev_down=False,
        decision_confidence=0.9,
        low_confidence_threshold=0.5,
    )
    assert r == Rung.RUN


def test_degraded_execution_health_reduces_even_with_high_confidence():
    r = select_rung(
        risk_kill=False,
        decision_late=False,
        jev_down=False,
        decision_confidence=0.9,
        low_confidence_threshold=0.5,
        execution_health_score=0.4,  # "Degraded" zone (Broken=0 .. Optimal=3)
    )
    assert r == Rung.REDUCE


def test_healthy_execution_and_confidence_runs():
    r = select_rung(
        risk_kill=False,
        decision_late=False,
        jev_down=False,
        decision_confidence=0.9,
        low_confidence_threshold=0.5,
        execution_health_score=2.7,  # "Optimal" zone
    )
    assert r == Rung.RUN


def test_execution_health_never_overrides_kill_or_late():
    r = select_rung(
        risk_kill=True,
        decision_late=False,
        jev_down=False,
        decision_confidence=0.9,
        low_confidence_threshold=0.5,
        execution_health_score=3.0,
    )
    assert r == Rung.KILL
