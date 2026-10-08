import pytest

from jevloop.battery import build_questions
from jevloop.split import (
    ALLOWED_QUESTIONS,
    SplitViolation,
    assert_split_respected,
    render_split_table,
)


def test_the_real_battery_respects_the_split():
    # build_questions() already calls assert_split_respected internally;
    # calling it here too pins the behaviour independently of that wiring.
    assert_split_respected(build_questions())


def test_every_allowed_question_is_a_judgment_not_a_calculation():
    for qid, (_qtype, description) in ALLOWED_QUESTIONS.items():
        assert "calculate" not in description.lower()
        assert "compute" not in description.lower()


def test_unknown_question_id_is_rejected():
    with pytest.raises(SplitViolation):
        assert_split_respected(
            {"a_new_thing": {"type": "noul", "instructions": "is this ok"}}
        )


def test_arithmetic_instructions_are_rejected_even_on_an_allowed_id():
    with pytest.raises(SplitViolation):
        assert_split_respected(
            {
                "regime": {
                    "type": "choice",
                    "instructions": "Please calculate the regime.",
                    "criteria": {},
                }
            }
        )


def test_vwap_request_is_rejected():
    with pytest.raises(SplitViolation):
        assert_split_respected(
            {
                "quote_environment": {
                    "type": "score",
                    "instructions": "What is the VWAP right now?",
                    "criteria": [],
                }
            }
        )


def test_structured_instructions_are_also_scanned():
    # instructions can be a dict with the question in one field and data in
    # others (see the API docs); the scanner must look inside it too.
    with pytest.raises(SplitViolation):
        assert_split_respected(
            {
                "toxic_flow": {
                    "type": "noul",
                    "instructions": {
                        "data": {"mid": 100},
                        "question": "Compute the exact value of the spread.",
                    },
                }
            }
        )


def test_legitimate_judgment_passes():
    assert_split_respected(
        {
            "toxic_flow": {
                "type": "noul",
                "instructions": "Is the aggressive flow informed rather than noise?",
            }
        }
    )


def test_render_split_table_names_both_sides():
    table = render_split_table()
    assert "DETERMINISTIC" in table
    assert "PROBABILISTIC" in table
    assert "state.py" in table
    assert "battery.py" in table
