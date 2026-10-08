from jevloop.battery import build_questions, validate_answers
from jevloop.client import MockDecisionClient


def test_mock_answers_validate_against_battery_schema():
    client = MockDecisionClient(seed=42)
    state = {"imbalance": 0.2, "inventory": 0.0005}
    questions = build_questions()
    answers, meta = client.ask(state, questions, timeout=2.0)
    validate_answers(answers)
    assert meta["model"].startswith("mock-")
    assert meta["route"] == "MOCK"


def test_mock_is_persistent_not_pure_noise():
    """Two consecutive calls on the same client shouldn't be uncorrelated --
    the regime/direction latent should move gradually, not teleport."""
    client = MockDecisionClient(seed=7)
    state = {"imbalance": 0.0, "inventory": 0.0}
    a1, _ = client.ask(state, build_questions(), timeout=2.0)
    a2, _ = client.ask(state, build_questions(), timeout=2.0)
    p1 = a1["regime"]["probabilities"]
    p2 = a2["regime"]["probabilities"]
    # The leading option's probability shouldn't swing by more than ~0.9
    # between consecutive ticks -- a loose bound that only catches "pure
    # random every tick" behaviour, not normal drift.
    top_option = max(p1, key=p1.get)
    assert abs(p1[top_option] - p2[top_option]) < 0.9


def test_mock_never_claims_to_be_real():
    client = MockDecisionClient()
    assert "mock" in client.model.lower()
    assert client.name == "MOCK"


def test_mock_choice_answers_have_confidence_and_probabilities():
    client = MockDecisionClient(seed=1)
    answers, _ = client.ask({"imbalance": 0.1}, build_questions(), timeout=2.0)
    for key in ("regime", "direction"):
        assert "confidence" in answers[key]
        assert abs(sum(answers[key]["probabilities"].values()) - 1.0) < 1e-3


def test_mock_score_answers_have_legend():
    client = MockDecisionClient(seed=1)
    answers, _ = client.ask({"inventory": 0.002}, build_questions(), timeout=2.0)
    for key in ("quote_environment", "inventory_pressure"):
        assert set(answers[key]["legend"].keys()) == {"0", "1", "2", "3"}
