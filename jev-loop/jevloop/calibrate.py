"""Calibration: does 80% mean 80% on this venue?

Reads the tick log, pairs each up/down `direction` call and Jev's own
confidence in it with the realised price move N ticks later, and reports a
Brier score plus a 10-bin reliability table (stated confidence vs how often
calls at that confidence were right). Writes reliability.png if matplotlib
happens to be installed; otherwise the table alone is enough.

This is not "did Jev predict price" in isolation. It is the honest check
the article insists on: if the model says 80%, does 80% actually happen.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
LOG_FILE = LOG_DIR / "log.jsonl"


def load_ticks() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    ticks = []
    with LOG_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                ticks.append(json.loads(line))
    return ticks


def pair_predictions(ticks: list[dict], horizon: int = 5) -> list[tuple[float, int]]:
    """(stated confidence, was the call right) pairs for every up/down
    direction call, checked `horizon` ticks later.

    The confidence is Jev's own confidence in that direction call
    (`direction_conf` in the log), nothing else. A "neutral" call makes no
    up/down claim, so it is left out rather than scored as 50%. A tick
    whose outcome price is missing (market closed) is skipped. Logs written
    before `direction_conf` existed carry no direction confidence at all
    and produce no pairs: there is no honest number to score them on."""
    pairs = []
    for i, t in enumerate(ticks):
        direction = t.get("direction")
        conf = t.get("direction_conf")
        if direction not in ("up", "down") or conf is None:
            continue
        j = i + horizon
        if j >= len(ticks):
            continue
        start, end = t.get("mid"), ticks[j].get("mid")
        if start is None or end is None or end == start:
            continue  # no move, nothing to score
        moved_up = end > start
        correct = 1 if moved_up == (direction == "up") else 0
        pairs.append((float(conf), correct))
    return pairs


def brier_score(pairs: list[tuple[float, int]]) -> float:
    if not pairs:
        return float("nan")
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def reliability_table(pairs: list[tuple[float, int]], n_bins: int = 10) -> list[dict]:
    bins = [[] for _ in range(n_bins)]
    for p, y in pairs:
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append((p, y))
    rows = []
    for i, b in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if b:
            mean_pred = sum(p for p, _ in b) / len(b)
            empirical = sum(y for _, y in b) / len(b)
        else:
            mean_pred, empirical = float("nan"), float("nan")
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(b), "mean_predicted": mean_pred, "empirical": empirical})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop calibrate")
    parser.add_argument("--horizon", type=int, default=5, help="ticks ahead to check the realised outcome")
    args = parser.parse_args(argv)

    ticks = load_ticks()
    if not ticks:
        print(f"No log found at {LOG_FILE}. Run `jev-loop run --ticks 60` first.")
        return 1

    pairs = pair_predictions(ticks, horizon=args.horizon)
    if not pairs:
        print(
            "Not enough up/down direction calls with a logged confidence yet. "
            "Run a longer session (logs from before this update carry no "
            "direction confidence and cannot be scored)."
        )
        return 1

    score = brier_score(pairs)
    print(f"\ncalibration over {len(pairs)} decisions, horizon {args.horizon} ticks")
    print(f"Brier score: {score:.4f} (0 = perfect, 0.25 = coin flip, 1 = always wrong)\n")
    print(f"{'bin':>10} {'n':>5} {'stated conf':>15} {'hit rate':>10}")
    for row in reliability_table(pairs):
        mp = f"{row['mean_predicted']:.2f}" if row["n"] else "-"
        emp = f"{row['empirical']:.2f}" if row["n"] else "-"
        print(f"{row['bin']:>10} {row['n']:>5} {mp:>15} {emp:>10}")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        rows = reliability_table(pairs)
        xs = [i / len(rows) + 0.5 / len(rows) for i in range(len(rows))]
        ys = [r["empirical"] for r in rows]
        fig, ax = plt.subplots(figsize=(5, 5))
        ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="perfectly calibrated")
        ax.plot(xs, ys, marker="o", label="this run")
        ax.set_xlabel("stated confidence in the direction call")
        ax.set_ylabel("how often the call was right")
        ax.set_title("Reliability: does 80% mean 80%?")
        ax.legend()
        out = LOG_DIR / "reliability.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print(f"\nwrote {out}")
    except ImportError:
        print("\n(matplotlib not installed, skipping reliability.png; the table above is the same data)")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
