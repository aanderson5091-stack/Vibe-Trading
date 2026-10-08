---
name: jev-loop
description: A 24/7 paper-trading loop for Alpaca, any crypto pair or US equity. Every tick computes a deterministic state snapshot, fires a seven-question judgment battery at TypeSafe's Jev decision model (the Vercel AI Gateway is the normal route, a direct TypeSafe key is a faster optional extra, or a labelled mock), composes an action from your own strategy.py thresholds, prices with Avellaneda-Stoikov, checks nine hard risk limits, and executes on Alpaca paper by default. Live trading exists behind a deliberately awkward three-gate opt-in, off unless all three are set. Includes a live HTML dashboard and a calibration report.
---

# jev-loop

Install location: `~/.claude/skills/jev-loop/`.
Framework: Roan (@RohOnChain), "How to Use Jev to Build a 24/7 HFT Trading System". Installed as a Claude Code skill by Lewis Jackson.

## The split

Deterministic layer (your code): exact arithmetic (mid-price, spread, book
imbalance), hard metrics (inventory, drawdown, session VWAP), safety and
policy (stop-losses, risk vetoes, routing orders to the book).

Probabilistic layer (Jev): fuzzy conditions (trending, mean reverting,
chaotic), order quality (is flow toxic or noise), execution health (is
the setup optimal or degrading). Seven typed questions, one call, one
latency, none of them arithmetic and none of them "what should I do."

```
uv run python -m jevloop explain-split
```

prints the full two-column table with the file that owns each row.

## Invocation

Natural language, or directly:

```
cd ~/.claude/skills/jev-loop
uv run python -m jevloop run --paper --ticks 60 --symbol BTC/USD
uv run python -m jevloop run --paper --mock            # force the mock client (never places orders)
uv run python -m jevloop run --paper --dry-execution   # real data and a real battery, no orders sent
uv run python -m jevloop run --paper --forever         # run continuously, Ctrl+C or `kill <pid>` to stop
uv run python -m jevloop run --paper --forever --bar 30m  # one decision per 30-minute bar close (1m/5m/15m/30m/1h)
uv run python -m jevloop validate-symbol AAPL          # resolve any symbol before running on it
uv run python -m jevloop explain-split                 # the deterministic vs probabilistic table
uv run python -m jevloop calibrate                     # Brier score + reliability table
uv run python -m jevloop serve                         # dashboard at http://127.0.0.1:8765
uv run python -m jevloop pause                         # same as Stop on the dashboard: no new orders
uv run python -m jevloop resume                        # same as Start on the dashboard
```

**Start / Stop.** The dashboard has a Start and a Stop button and shows
RUNNING or PAUSED. Stop pauses new orders: the loop keeps reading the
market, asking Jev and updating the dashboard, pulls this run's resting
quotes, and places nothing until Start. It does not close positions. The
KILL switch (the hard risk limits) does that, and still fires while
paused. The state is kept in `~/.jev-loop/control.json`, so it survives a
page refresh and a restart, and the loop reads it before every order.
The buttons only work from the page `jevloop serve` hands out on
127.0.0.1: each press carries a per-install token from
`~/.jev-loop/control.token`, and the server sends no CORS headers.

`--ticks 0` means the same thing as `--forever`. Both cancel the resting
orders this run placed (and nothing else on the account) before exiting.

Ask Claude things like:

- "run the jev-loop skill for 100 ticks on ETH/USD"
- "run jev-loop on AAPL and tell me if the market's open"
- "run jev-loop in mock mode so I can see the dashboard without spending a key"
- "keep jev-loop running continuously in the background"
- "calibrate the jev-loop and show me the Brier score"
- "explain the split in jev-loop"

## Any asset

`--symbol` accepts any crypto pair (`BTC/USD`, `ETH/USD`, 24/7) or any US
equity ticker (`AAPL`, `SPY`, `TSLA`, `NVDA`, market hours only). The
default stays `BTC/USD` because it never closes. `jevloop/assets.py`
resolves the symbol into a spec: which Alpaca endpoints to call, the
minimum order notional, quantity precision, whether shorting is allowed,
and whether the venue is open right now. Orders are sized from a dollar
target (`notional_usd` in `limits.py`), not a fixed quantity, so the same
config works whether the asset is an $85,000 coin or a $30 stock.

Equities have no Level 2 depth on the basic feed: the state snapshot uses
best bid/ask and recent trades only, and marks the missing depth fields
absent rather than inventing them. When the equity market is closed, the
loop holds and never places an order; it says so on the tick line rather
than silently doing nothing.

## Your strategy lives in strategy.py

Everything this loop ships with is a harness, not an edge: a generic
strategy pulled out of thin air, wired in only so a demo run shows real
fills. `jevloop/strategy.py` is the one file built for you to edit --
it owns the seven tunable thresholds `compose_action()` reads (when to
pull quotes, widen, quote both sides or wide, and how confident Jev has
to be before a directional leg is taken), plus an `apply_strategy()` hook
called on every tick with the action already chosen, free to change it
or veto it outright. The shipped default matches exactly what the video
ran: change nothing here and nothing changes. The hard risk caps stay
separate, in `jevloop/limits.py`, and a strategy can never raise them,
only add more caution on top.

## What each file does

| File | Job |
|---|---|
| `jevloop/strategy.py` | The file you edit: the seven tunable thresholds behind `compose_action()`, plus the `apply_strategy()` hook to override or veto an action. Shipped default changes nothing. |
| `jevloop/limits.py` | The hard risk caps and operational numbers. Never overridable by a strategy. |
| `jevloop/assets.py` | Resolves any symbol into a spec: endpoints, notional floor, precision, shorting, market hours. |
| `jevloop/state.py` | Deterministic state snapshot, under ~400 tokens, real price history with the venue's own timestamps (one-minute bars backfilled at startup plus a mid sample every tick), each trade counted once in session VWAP, drawdown measured against real account equity, honest depth degradation. |
| `jevloop/split.py` | The allow-list of battery questions and the guard that refuses any question that looks like arithmetic. |
| `jevloop/battery.py` | The seven-question Jev battery (regime, direction, toxic flow, liquidity stress, quote environment, inventory pressure, execution health). |
| `jevloop/client.py` | Resolves the decision client: Vercel AI Gateway (the normal route) or a direct TypeSafe key (a faster optional extra) or a mock, prints which one won, pins and logs the model per response. Also the Jev call throttle (`JevThrottle`): one shared client, one call at a time, rate-limit backoff. |
| `jevloop/policy.py` | `compose_action()`, code, not Jev, turns seven answers into KILL / PULL_QUOTES / WIDEN / QUOTE_BOTH_SIDES / QUOTE_WIDE / STAND_DOWN using strategy.py's thresholds, plus a directional leg, then hands the result to strategy.py's hook. |
| `jevloop/pricing.py` | Avellaneda-Stoikov reservation price and half spread, worked out in basis points of mid so the spread scales with the price. |
| `jevloop/risk.py` | Nine hard limits, checked before every order, never delegated. |
| `jevloop/ladder.py` | The five-rung fallback ladder (RUN / REDUCE / HOLD_LATE / RULES_ONLY / KILL). |
| `jevloop/execution/alpaca.py` | Alpaca execution, crypto or equities. Paper by default; live trading exists only behind the three-gate opt-in (see Live trading below). Refuses to place an equity order while the market is closed. |
| `jevloop/loop.py` | The nine-stage block loop, one JSON line per tick to `~/.jev-loop/log.jsonl` and `~/.jev-loop/latest.json`. Supports `--forever` / `--ticks 0` with a clean shutdown that cancels only this run's resting orders. Fills are read back from Alpaca, never assumed; KILL sends a real closing order. |
| `jevloop/calibrate.py` | Brier score + 10-bin reliability table from the log, scoring Jev's confidence in each up/down call against whether price moved that way; `reliability.png` if matplotlib is present. |
| `jevloop/serve.py` | Tiny static server for `dashboard/index.html` and `dashboard/wall.html`, 127.0.0.1 only, plus the token-checked Start/Stop endpoint (`/control`). |
| `jevloop/control.py` | The RUNNING / PAUSED state file and the per-install token. The loop checks it before every order. |
| `dashboard/*.html` | Two live dashboards, polling `latest.json`. No simulation. `index.html` has the Start / Stop buttons. |
| `tests/` | pytest for policy thresholds, risk vetoes, the ladder, the Jev call throttle, state maths, the asset resolver, the split guard, the mock client's shape, the Alpaca paper-URL guard, the three-gate live-trading check, Start / Stop (pause blocks orders, resume, the token check), and the member-reported fixes (position cap counts working buys, spread in bps, stuck positions closed, clean-up on every exit, fees, rounded-down sells, network timeouts). |

## Setup

1. Copy `.env.example` to `.env` in this folder and fill in what you have.
2. `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are required, paper keys only,
   from https://app.alpaca.markets/paper/dashboard/overview. Paper keys
   start with `PK`.
3. `AI_GATEWAY_API_KEY` is the normal way to reach Jev: works today, no
   invite or waitlist, but the Vercel team needs a card on file before
   the gateway will serve requests, even the free credits. A few dollars
   covers roughly a month at this tick rate. `TYPESAFE_API_KEY` is an
   optional extra, slightly faster (one less hop), useful only if you are
   already off TypeSafe's waitlist. Without either, the loop runs on a
   clearly-labelled mock decision client so nothing above blocks on a
   billing page or a waitlist. Mock runs are always dry: no orders.
   Jev is called on a throttle, not every tick: see "Jev call rate" below.
4. `DEFAULT_SYMBOL` is optional; it sets what `--symbol` defaults to if
   you don't pass one.

## Live trading (opt-in, off by default)

Paper is the default everywhere in this skill. Turning on live trading
needs all three of the following, together, every time:

1. the `--live` flag on the command line
2. `JEV_LOOP_ALLOW_LIVE=i-understand-the-risk` in the environment
3. typing the exact confirmation phrase the CLI asks for at startup

Missing any one of the three refuses to start; it never falls back to
paper silently. All three present routes every order to Alpaca's live
endpoint instead of paper, with real money and no safety net beyond the
risk caps in `limits.py`, which still apply and are not raised by going
live. This is the "flip of a switch" the video describes, made
deliberately awkward on purpose so it only ever happens on purpose. See
README.md for the exact commands.

## Dependencies

`uv`-managed virtual environment under `.venv/` with Python 3.10+ and:

- `requests>=2.31`
- `python-dotenv>=1.0`
- `matplotlib>=3.8` (optional, only for `reliability.png`)
- `pytest>=8.0` (dev only, for `tests/`)

## What this is not

Paper by default, everywhere. Live trading exists only behind the
three-gate opt-in above and starts with the same small dollar caps as
paper. This does not claim a profit. It cannot turn a bad strategy into
a good one: Jev makes seven judgments cheap and fast, edge is still
yours, and yours lives in strategy.py.
