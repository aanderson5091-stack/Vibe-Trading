# jev-loop

A 24/7 paper-trading loop built around a Jev decision battery, for the video
*How to Use Jev to Build a 24/7 HFT Trading System*.

The split, in one sentence: code computes the state, Jev answers seven typed
questions about it in one call, your code composes those answers into an
action using thresholds you set in `strategy.py`, a risk engine can veto
any of it, and execution defaults to Alpaca's paper API everywhere.

## Quick start

```bash
cd ~/.claude/skills/jev-loop
cp .env.example .env        # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY at minimum
uv run pytest -q             # tests, no network needed
uv run python -m jevloop explain-split         # the split, as a table
uv run python -m jevloop validate-symbol AAPL  # resolve any symbol first
uv run python -m jevloop run --paper --ticks 30 --symbol BTC/USD
uv run python -m jevloop serve   # open http://127.0.0.1:8765
```

## Start and Stop

The dashboard has a **Start** and a **Stop** button, and shows **RUNNING**
or **PAUSED** next to them.

- **Stop** pauses new orders. The loop keeps running: it still reads the
  market, asks Jev, writes the log and updates the dashboard. It pulls
  the quotes this run has resting so nothing new fills, then places no
  orders until you press Start.
- **Stop does not close positions.** Whatever you hold stays held. The
  KILL switch (the hard risk limits, such as the drawdown cap) is what
  closes a position, and it still works while paused.
- **Start** lets it place orders again from the next tick.
- The state is saved in `~/.jev-loop/control.json`, so it stays the same
  after a page refresh or a restart. The loop checks it before every order.
- No dashboard open? `uv run python -m jevloop pause` and
  `uv run python -m jevloop resume` do the same thing from a terminal.

The buttons only work from the page `jevloop serve` gives you at
http://127.0.0.1:8765. The server only listens on your own machine, each
press is a POST carrying a random token made for your install (in
`~/.jev-loop/control.token`), and it sends no CORS headers, so another
website open in your browser can't press Stop or Start for you. Same on
Mac, Windows and Linux.

`--symbol` takes any crypto pair (24/7) or US equity ticker (market hours
only); `jevloop/assets.py` resolves it into endpoints, order-size rules,
and session rules. Orders are sized from a dollar target, not a fixed
quantity, so the same defaults work across assets.

No `AI_GATEWAY_API_KEY` or `TYPESAFE_API_KEY`? The loop runs on a
clearly-labelled mock decision client. It says so on the first line. The
mock answers with random numbers, so it never places orders, not even on
paper: a mock run is always a dry run.

## Running continuously

A plain run stops after `--ticks N`. To keep it going:

```bash
uv run python -m jevloop run --paper --forever --symbol BTC/USD
# or, equivalently:
uv run python -m jevloop run --paper --ticks 0 --symbol BTC/USD
```

Add `--bar 30m` (or `1m`, `5m`, `15m`, `1h`) to decide once per bar close,
UTC-aligned, instead of every 2 s. Returns and volatility still come from
real one-minute bars, refreshed at each close.

Stop it with Ctrl+C in the foreground. In the background:

```bash
nohup uv run python -m jevloop run --paper --forever --symbol BTC/USD \
  > ~/.jev-loop/continuous.log 2>&1 &
echo $! > ~/.jev-loop/continuous.pid
# ...later...
kill "$(cat ~/.jev-loop/continuous.pid)"
```

On Windows PowerShell there is no `nohup` or `kill`, and `jevloop` needs
the keys from `.env` loaded by uv. Run it in its own minimised window and
stop it with Ctrl+C in that window (a `Stop-Process` is a hard stop that
skips the order clean-up below):

```powershell
Start-Process powershell -WindowStyle Minimized -WorkingDirectory "$HOME\.claude\skills\jev-loop" -ArgumentList "-NoExit", "-Command", "uv run --env-file .env python -m jevloop run --paper --forever"
```

Either way, on Ctrl+C or `kill` the loop cancels the resting orders it
placed itself before it exits rather than leaving them open. It never
touches other orders on your Alpaca account. The dashboard (`jevloop
serve`) keeps reading the same `~/.jev-loop/latest.json` regardless of
whether the run is bounded or continuous.

## Your strategy lives in strategy.py

Everything this loop ships with is a harness, not an edge: a generic
strategy pulled out of thin air, wired in only so a demo run shows real
fills. `jevloop/strategy.py` owns the seven tunable thresholds behind
`compose_action()` and an `apply_strategy()` hook that gets one last
look at every action before it goes near an order, free to change or
veto it. The shipped default matches exactly what the video ran.

## The nine-stage loop

```
block event -> read the book -> state snapshot -> battery (seven Jev judgments)
  -> policy engine (strategy.py's thresholds + hook) -> pricing (Avellaneda-Stoikov, your code)
  -> risk veto (your code, absolute) -> post-only quotes + directional leg
  -> log, fills, inventory
```

## Jev call rate

The Vercel AI Gateway allows roughly 20 to 30 requests a minute. A loop
that calls Jev every tick (every 2s, or every 500ms if you shorten the
tick) runs straight into "access frequency is too high". So the loop
never does that:

- The fast loop runs every tick on price and risk, and reuses the last
  Jev judgment between calls.
- Jev is called at most once every `JEV_MIN_INTERVAL_S` seconds (default
  4, so 15 a minute), or sooner when the state changes materially: price
  or spread moves `JEV_MATERIAL_MOVE_BPS` (default 15 bps) or the
  position flips. Even then never more often than `JEV_MIN_GAP_S`
  (default 3s, 20 a minute).
- On a 429 or a "frequency" error the loop backs off exponentially (5s,
  10s, 20s, up to 120s) and keeps the last judgment meanwhile.
- A judgment older than `JEV_MAX_JUDGMENT_AGE_S` (default 60s) is not
  reused: the ladder drops to RULES_ONLY until Jev answers again.
- One shared client, one call at a time. Never parallel Jev calls, never
  a second client or a thread pool.

A direct `TYPESAFE_API_KEY` allows higher rates than the gateway, so with
one you can lower `JEV_MIN_INTERVAL_S` and `JEV_MIN_GAP_S` in `.env`.

## The fallback ladder

```
healthy + high confidence -> RUN
healthy + low confidence  -> REDUCE
late past the tick budget -> HOLD_LATE (never quote on stale state)
Jev unavailable            -> RULES_ONLY (deterministic fallback, no model)
hard limit breached        -> KILL (cancel own orders, market-close the position it built, stop)
```

## Live trading (opt-in, off by default)

Paper is the default everywhere: `execution/alpaca.py` will not reach
Alpaca's live endpoint unless all three of the following are true at
once.

```bash
export JEV_LOOP_ALLOW_LIVE=i-understand-the-risk
uv run python -m jevloop run --live --ticks 30 --symbol BTC/USD
# then type the exact confirmation phrase the CLI prints and asks for
```

Windows PowerShell sets the variable with
`$env:JEV_LOOP_ALLOW_LIVE = "i-understand-the-risk"` and runs
`uv run --env-file .env python -m jevloop run --live --ticks 30 --symbol BTC/USD`.

Missing the environment variable, missing `--live`, or typing anything
other than the exact confirmation phrase refuses to start; it never
falls back to paper silently. This is real money with no paper safety
net, at the same small dollar caps `limits.py` enforces on paper -- a
strategy can never raise them. Treat `--live` as what it is: a flip of
a switch made deliberately awkward so it only ever happens on purpose.

## Safety

- Paper by default, everywhere. Live trading exists only behind the
  three-gate opt-in above.
- The hard risk caps live in `jevloop/limits.py` and never change between
  paper and live. The tunable strategy thresholds live in `jevloop/strategy.py`
  instead -- change a number, restart, see different behaviour.
- The risk engine (`jevloop/risk.py`) never calls Jev and never delegates,
  and runs after `strategy.py`'s hook has had its say, not before.
- Long or flat only. Alpaca crypto is spot and this account can't short,
  so a SELL signal closes the long this run holds (never more than Alpaca
  says is held) and does nothing when flat ("no position to close").
- Stop on the dashboard pauses new orders and never closes a position;
  the KILL switch closes positions and still works while paused.
- This is not investment advice and it does not claim a profit. It is a
  scaffold for a decision battery, a policy engine, and a risk layer around
  a fast model. Edge is still your job, and it lives in `strategy.py`.
