"""jev-loop: a 24/7 trading loop built around a Jev decision battery.

Code computes the state. Jev answers seven typed questions about it. Code
(policy.py, using strategy.py's thresholds and hook) decides. Code
(risk.py, using limits.py's hard caps) can veto anything. Execution
(execution/alpaca.py) defaults to Alpaca's paper API everywhere; live
trading exists only behind a deliberately awkward three-gate opt-in.
"""

__version__ = "0.1.0"
