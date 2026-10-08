"""The split, enforced.

Deterministic layer (your code): exact arithmetic, hard metrics, safety
and policy. Probabilistic layer (Jev): fuzzy conditions, order-quality
judgments, execution-health judgments. Everything else in this scaffold
is built around that split; this file is the guard that keeps the second
half honest. It is the one place a battery question is allowed to be
defined, and it refuses anything whose instructions look like a request
for arithmetic rather than a judgment.
"""

from __future__ import annotations

# question id -> (Jev question type, the judgment it makes, not a calculation)
ALLOWED_QUESTIONS: dict[str, tuple[str, str]] = {
    "regime": (
        "choice",
        "is the market trending, mean reverting, high vol, or in crisis",
    ),
    "direction": (
        "choice",
        "price bias over the next few ticks, a judgment, not a forecast formula",
    ),
    "toxic_flow": ("noul", "is the aggressive flow informed rather than noise"),
    "liquidity_stressed": ("noul", "is the book thinner than its recent norm"),
    "quote_environment": (
        "score",
        "how favourable this state is for providing liquidity",
    ),
    "inventory_pressure": ("score", "how urgent it is to cut the current position"),
    "execution_health": ("score", "whether execution quality is optimal or degrading"),
}

# Substrings that mean "this is asking Jev to do arithmetic", banned in any
# question's instructions. Deliberately conservative: false positives (a
# legitimate judgment question that happens to use one of these words) are
# a cheap price for never silently shipping an arithmetic question to a
# model that is supposed to answer judgments, not calculate.
_ARITHMETIC_MARKERS = (
    "calculate",
    "compute the",
    "what is the exact",
    "sum of",
    "average of",
    "mean of",
    "add up",
    "multiply",
    "divide by",
    "vwap",
    "moving average",
    "standard deviation",
    "variance of",
    "exact value",
    "precise value",
    "spread in bps",
    "mid price of",
)


class SplitViolation(Exception):
    """Raised when a question sent to Jev is off the allow-list, or its
    instructions look like a request for a computable quantity instead of
    a judgment."""


def _instructions_text(instructions) -> str:
    if isinstance(instructions, str):
        return instructions
    if isinstance(instructions, dict):
        return " ".join(_instructions_text(v) for v in instructions.values())
    if isinstance(instructions, list):
        return " ".join(_instructions_text(v) for v in instructions)
    return str(instructions)


def assert_split_respected(questions: dict) -> None:
    """Raise SplitViolation if any question is off the allow-list, or its
    instructions contain an arithmetic marker. Call this on every battery
    before it is sent, not just once at startup: a question dict built at
    runtime is exactly the case this guard exists for."""
    for qid, question in questions.items():
        if qid not in ALLOWED_QUESTIONS:
            raise SplitViolation(
                f"question '{qid}' is not on the allow-list in jevloop/split.py. "
                "Add it there first, naming the judgment (not the calculation) it makes."
            )
        text = _instructions_text(question.get("instructions", "")).lower()
        for marker in _ARITHMETIC_MARKERS:
            if marker in text:
                raise SplitViolation(
                    f"question '{qid}' looks like it asks Jev to do arithmetic "
                    f"(found '{marker}'). Compute it in code (state.py) instead, and if "
                    "you need Jev's read on the result, ask a judgment question about it."
                )


def deterministic_rows() -> list[tuple[str, str]]:
    """(what your code owns, which module owns it)."""
    return [
        (
            "Exact arithmetic: mid-price, microprice, bid-ask spread, order book imbalance",
            "state.py",
        ),
        (
            "Hard metrics: live inventory, current drawdown, session VWAP",
            "state.py",
        ),
        (
            "Safety and policy: hard stop-losses, risk vetoes, routing limit orders to the book",
            "risk.py, policy.py, execution/alpaca.py",
        ),
    ]


def probabilistic_rows() -> list[tuple[str, str]]:
    """(what Jev owns, which battery question(s) implement it)."""
    return [
        (
            "Fuzzy conditions: trending, mean reverting, chaotic",
            "battery.py: regime, direction",
        ),
        (
            "Order quality: is incoming flow toxic/informed or just noise",
            "battery.py: toxic_flow, liquidity_stressed",
        ),
        (
            "Execution health: is setup quality optimal or has execution degraded",
            "battery.py: quote_environment, inventory_pressure, execution_health",
        ),
    ]


def render_split_table(width: int = 34) -> str:
    """Plain-text two-column table for the CLI and the install prompt.

    Each side lists its rows, wrapped to `width` characters, with the
    owning module(s) on the line under each row in brackets.
    """
    import textwrap

    def block(rows: list[tuple[str, str]]) -> list[str]:
        out: list[str] = []
        for desc, module in rows:
            out.extend(textwrap.wrap(desc, width))
            out.append(f"[{module}]")
            out.append("")
        return out

    left_lines = block(deterministic_rows())
    right_lines = block(probabilistic_rows())

    header_l = "DETERMINISTIC (your code)".ljust(width)
    header_r = "PROBABILISTIC (Jev)"
    rule = "-" * width
    lines = [f"{header_l}  {header_r}", f"{rule}  {rule}"]

    for i in range(max(len(left_lines), len(right_lines))):
        left = left_lines[i] if i < len(left_lines) else ""
        right = right_lines[i] if i < len(right_lines) else ""
        lines.append(f"{left.ljust(width)}  {right}")

    return "\n".join(lines).rstrip()
