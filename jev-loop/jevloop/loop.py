"""The nine-stage block loop.

    1. block event            (tick clock, "block" = one tick, default 2s)
    2. read the book          (Alpaca paper market data: L2 depth on crypto,
                               best bid/ask on equities, and recent trades)
    3. state snapshot         (deterministic, state.py)
    4. battery                (seven Jev judgments, one call, battery.py)
    5. policy engine          (compose_action, policy.py: code, not Jev)
    6. pricing                (Avellaneda-Stoikov, pricing.py: code)
    7. risk veto              (hard limits, risk.py: code, absolute veto)
    8. execute                (cancel-replace post-only quotes plus directional leg)
    9. log, fills, inventory  (JSONL and latest.json for the dashboard)

Paper by default. `--mock` forces the mock decision client even if a real
key is present, useful for a clean demo run that can never fail on network
or billing grounds. A mock answers with random numbers, so any mock run
(forced, or fallen back to mid-run) is also a dry run: no orders at all. `--dry-execution` reads real market data and fires a
real Jev battery but never submits an order to Alpaca; every fill line
reads "dry" instead. Useful for testing the full pipeline without touching
the paper account. `--ticks 0` or `--forever` runs continuously until
stopped (Ctrl+C, or a SIGTERM if it is running in the background), and
cancels the resting orders it placed (only those, matched on this run's
client_order_id prefix) before it exits rather than leaving them behind. `--live` is a separate, deliberately awkward opt-in documented in
execution/alpaca.py and SKILL.md; paper is what every default here
resolves to unless a caller goes out of its way to ask for live.

Any asset `jevloop/assets.py` resolves: a crypto pair runs 24/7; a US
equity ticker only trades while the market is open, and the loop holds
(never orders) while it is closed, per `execution/alpaca.py`'s market-hours
guard.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

from . import control
from .assets import AssetSpec, UnknownSymbolError, resolve_symbol, size_order
from .battery import run_battery
from .client import (
    DecisionClientError,
    GatewayVerificationRequired,
    JevThrottle,
    RateLimited,
    resolve_decision_client,
)
from .execution.alpaca import (
    LIVE_CONFIRMATION_PHRASE,
    AlpacaAPIError,
    AlpacaConfigError,
    LiveTradingRefused,
    MarketClosedError,
    client_from_env,
)
from .ladder import Rung, select_rung
from .limits import Limits
from .policy import (
    KILL,
    PULL_QUOTES,
    QUOTE_BOTH_SIDES,
    QUOTE_WIDE,
    STAND_DOWN,
    WIDEN,
    compose_action,
    fallback_action,
)
from .pricing import quote_prices
from .state import (
    InventoryState,
    apply_fill,
    build_snapshot,
    record_fill_slippage,
    update_vwap,
)

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
LOG_FILE = LOG_DIR / "log.jsonl"
LATEST_FILE = LOG_DIR / "latest.json"
LATEST_WINDOW = 120
HISTORY_S = 2400.0  # keep 40 minutes of real price history (return_30m needs 30; venues skip quiet minutes)
TRADE_TAPE_S = 60.0  # real trades kept for the flow fields (30s window + slack)
# --bar: decide once per bar close instead of every 2 s block.
BAR_CHOICES = {"1m": 60.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0, "1h": 3600.0}
BAR_SETTLE_S = 2.0  # wait this long past the close so the venue has stamped the bar
BAR_DECISION_DEADLINE_S = 5.0  # Jev's answer still has to come back fast in bar mode
TERMINAL_ORDER_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _parse_ts(raw: str) -> float:
    """RFC 3339 venue timestamp (Alpaca sends nanoseconds) -> epoch seconds."""
    from datetime import datetime

    raw = raw.replace("Z", "+00:00")
    if "." in raw:
        head, rest = raw.split(".", 1)
        frac, tz = rest[:9], ""
        for sep in ("+", "-"):
            if sep in rest:
                frac, tz = rest.split(sep, 1)
                tz = sep + tz
                break
        raw = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(raw).timestamp()


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def reconcile_fills(alpaca, inv: InventoryState, seen: dict, expected_px: dict, after_iso: str, now: float, open_buys: dict | None = None):
    """Read this run's orders back from Alpaca and fold in only the fill
    quantity that is new since the last check, at the broker's own average
    fill price. Returns (list of (side, qty, price) new fills, pending?)
    where pending means at least one of our orders is still working. If
    `open_buys` is given, open_buys["usd"] is set to the dollar value of
    this run's buy orders still working, so the position cap can count
    them before they fill."""
    new_fills = []
    pending = False
    working_buy_usd = 0.0
    for o in alpaca.get_own_orders(after_iso):
        oid = o.get("id")
        fq = float(o.get("filled_qty") or 0.0)
        avg = float(o.get("filled_avg_price") or 0.0)
        pq, pavg = seen.get(oid, (0.0, 0.0))
        if fq > pq + 1e-12 and avg > 0:
            dq = fq - pq
            px = (fq * avg - pq * pavg) / dq
            apply_fill(inv, o["side"], dq, px, now)
            exp = expected_px.get(o.get("client_order_id"))
            if exp:
                record_fill_slippage(inv, expected_price=exp, fill_price=px, side=o["side"])
            new_fills.append((o["side"], dq, px))
            seen[oid] = (fq, avg)
        if o.get("status") not in TERMINAL_ORDER_STATES:
            pending = True
            if o.get("side") == "buy":
                left = max(float(o.get("qty") or 0.0) - fq, 0.0)
                px_ = float(o.get("limit_price") or 0.0) or expected_px.get(o.get("client_order_id")) or avg
                working_buy_usd += left * (px_ or 0.0)
    if open_buys is not None:
        open_buys["usd"] = working_buy_usd
    return new_fills, pending


def sync_fees(inv: InventoryState, run_held: float, price: float) -> float:
    """Alpaca takes the fee on a crypto buy in the coin itself, so the
    account ends up holding a little less than the fills add up to. Left
    alone, the bot thinks it owns coin it doesn't, and its closing sells
    get rejected. `run_held` is what Alpaca says this run holds (account
    position minus whatever was there before the run started). When that
    is below the bot's own count on a long, the gap is the fee: shrink
    inventory to match and book the fee as a realised cost. Returns the
    fee in dollars (0.0 when nothing was taken)."""
    if inv.inventory <= 0 or run_held < 0 or run_held >= inv.inventory - 1e-12:
        return 0.0
    fee_qty = inv.inventory - run_held
    fee_usd = fee_qty * price
    inv.inventory = run_held
    if run_held <= 1e-12:
        inv.inventory = 0.0
        inv.entry_price = 0.0
        inv.position_opened_at = None
    inv.fees_usd += fee_usd
    inv.realised_pnl_usd -= fee_usd
    return fee_usd


def _floor_qty(qty: float, spec: AssetSpec) -> float:
    """Round a closing quantity DOWN to the asset's precision. Rounding up
    asks to sell more than is held, and Alpaca rejects it."""
    step = 10 ** spec.qty_precision
    return math.floor(round(qty * step, 6)) / step


def _flatten(alpaca, spec: AssetSpec, inv: InventoryState, dry: bool, seen: dict, expected_px: dict, after_iso: str, now: float) -> str:
    """KILL means flatten for real: cancel this run's own resting orders,
    then send a market order that closes the position this run built. It
    never sells more than Alpaca says is actually held, so it cannot open a
    short or close a position something else opened."""
    if dry:
        return f"dry: would cancel own orders and flatten {inv.inventory}"
    msgs = []
    try:
        n = alpaca.cancel_own_orders()
        msgs.append(f"cancelled {n} own order(s)")
        reconcile_fills(alpaca, inv, seen, expected_px, after_iso, now)
        held = alpaca.get_position_qty()
        bot = inv.inventory
        qty = min(abs(bot), abs(held)) if bot * held > 0 else 0.0
        qty = _floor_qty(qty, spec)
        if qty > 0:
            side = "sell" if bot > 0 else "buy"
            alpaca.submit_market_order(side, qty)
            for _ in range(5):
                time.sleep(0.5)
                fills, pending = reconcile_fills(alpaca, inv, seen, expected_px, after_iso, now)
                if not pending:
                    break
            msgs.append(f"market {side} {qty} sent, inventory now {inv.inventory}")
        else:
            msgs.append("nothing held to flatten")
    except (AlpacaAPIError, MarketClosedError) as exc:
        msgs.append(f"FLATTEN FAILED, close the position by hand in Alpaca: {exc}")
    return "; ".join(msgs)


def _close_position(alpaca, spec: AssetSpec, inv: InventoryState, mid: float, expected_px: dict | None) -> str:
    """Market order that closes the position this run built, never more
    than Alpaca says is held, rounded down so it is never rejected."""
    bot = inv.inventory
    held = alpaca.get_position_qty()
    qty = _floor_qty(min(abs(bot), abs(held)), spec) if bot * held > 0 else 0.0
    if qty <= 0:
        return "no position to close"
    side = "sell" if bot > 0 else "buy"
    inv.orders_submitted += 1
    o = alpaca.submit_market_order(side, qty)
    if expected_px is not None and o.get("client_order_id"):
        expected_px[o["client_order_id"]] = mid
    return f"sent market {side} {qty} (closing position)"


def _fmt_money(x: float) -> str:
    return f"{x:,.1f}"


class _StopRequested(Exception):
    """Raised by the SIGTERM handler so a run stopped from the background
    (`kill <pid>`) shuts down exactly as cleanly as a foreground Ctrl+C
    (KeyboardInterrupt) does: cancel resting orders, then exit."""


def _handle_sigterm(signum, frame) -> None:
    raise _StopRequested()


def run(
    symbol: str,
    ticks: int | None,
    mock: bool,
    limits: Limits,
    dry_execution: bool = False,
    live: bool = False,
    confirmation: str | None = None,
    bar_seconds: float | None = None,
) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        spec = resolve_symbol(symbol)
    except UnknownSymbolError as exc:
        print(f"cannot start: {exc}")
        return 1

    try:
        alpaca = client_from_env(
            symbol=spec.symbol, live=live, confirmation=confirmation
        )
    except AlpacaConfigError as exc:
        print(f"cannot start: {exc}")
        return 1
    except LiveTradingRefused as exc:
        print(f"cannot start: {exc}")
        return 1

    # One shared client for the whole run, and one throttle in front of it.
    client = resolve_decision_client(mock=mock)
    throttle = JevThrottle()
    print(
        f"jev calls: at most one every {throttle.min_interval_s:g}s (sooner on a material "
        f"change, never under {throttle.min_gap_s:g}s), last judgment reused in between"
    )
    if client.name == "MOCK" and not dry_execution:
        # A mock answers with random numbers. Random numbers never place
        # orders, not even on paper: the mock is for exercising the pipeline.
        dry_execution = True
        print("mock decision client: forcing dry execution, no orders will be sent.")
    session_txt = "24/7" if spec.is_24_7 else "market hours only"
    print(
        f"asset: {spec.symbol} ({spec.asset_class}, {session_txt}, "
        f"min order ${spec.min_notional_usd:.2f}, depth: {'yes' if spec.has_depth else 'best bid/ask only'})"
    )
    # Bar mode: sleep to each bar close, then run one block. Otherwise one
    # block every limits.tick_seconds. Jev's deadline stays short either way.
    cadence_s = 0.0 if bar_seconds else limits.tick_seconds
    deadline_s = min(bar_seconds, BAR_DECISION_DEADLINE_S) if bar_seconds else limits.tick_seconds
    if bar_seconds:
        print(
            f"tick cadence: one decision at each {bar_seconds / 60:g}-minute bar close "
            f"(UTC-aligned), Jev deadline {deadline_s:.0f}s"
        )
    else:
        print(
            f"tick cadence: one block every {limits.tick_seconds:.1f}s "
            f"(Alpaca calls capped at {limits.max_alpaca_calls_per_minute}/min)"
        )
    print(
        "your strategy lives in strategy.py (edit thresholds, or the apply_strategy hook)"
    )
    if ticks is None:
        print(
            "running continuously until stopped (Ctrl+C, or `kill <pid>` if this is "
            "in the background). Resting orders are cancelled on shutdown."
        )
    if dry_execution:
        print(
            "dry execution: real market data and a real Jev battery, no orders sent to Alpaca."
        )
    if control.is_paused(LOG_DIR):
        print(
            "control: PAUSED from the dashboard, no new orders until you press Start "
            "(or run: uv run python -m jevloop resume)."
        )

    # Seed equity from the real account so drawdown is measured against real
    # money. If the account cannot be read (dry run without a working key),
    # fall back to the position cap, the most this loop can ever put at risk.
    try:
        start_equity = float(alpaca.get_account().get("equity") or 0.0)
    except AlpacaAPIError as exc:
        start_equity = 0.0
        print(f"could not read account equity ({exc.status_code}); ", end="")
    if start_equity <= 0:
        start_equity = limits.max_position_usd
        print(f"drawdown measured against the ${start_equity:.2f} position cap")
    else:
        print(f"account equity at start: ${start_equity:,.2f}")
    inv = InventoryState(equity_usd=start_equity, high_water_mark_usd=start_equity)
    # Whatever the account already held in this symbol before the run is
    # not the loop's: fees are measured against it, and it is never sold.
    start_held = 0.0
    if not dry_execution:
        try:
            start_held = alpaca.get_position_qty()
        except AlpacaAPIError:
            pass
    open_buys = {"usd": 0.0}
    session_start = time.time()
    session_start_iso = _iso(session_start - 5)
    fills_seen: dict = {}
    expected_px: dict = {}
    reconcile_needed = False

    # Real price history. Backfill 31 minutes of one-minute bars (stamped at
    # each bar's close) so returns and volatility are real from the first
    # tick, then add one real mid sample per tick with the time it was read.
    price_hist: list[tuple[float, float]] = []
    try:
        price_hist = _minute_bar_history(alpaca, session_start)
        print(f"price history: {len(price_hist)} one-minute bars backfilled")
    except (AlpacaAPIError, KeyError, ValueError) as exc:
        print(f"price history: no backfill ({exc}); returns fill in as ticks arrive")
    trade_tape: list[tuple[float, float, float, str]] = []  # (ts, price, size, side)
    seen_trade_ids: set = set()
    trades_since_iso = _iso(session_start - TRADE_TAPE_S)
    api_error_streak = 0
    jev_down = False
    rest_counter = 0
    resting_quotes: dict | None = None
    recent_ticks: list[dict] = []
    block = 0
    started_at = time.time()

    n = 0
    previous_sigterm_handler = None
    try:
        previous_sigterm_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, AttributeError, OSError):
        pass  # not the main thread, or a platform without SIGTERM

    try:
        while ticks is None or n < ticks:
            if bar_seconds:
                time.sleep(_seconds_to_bar_close(time.time(), bar_seconds))
            tick_start = time.monotonic()
            block += 1
            now = time.time()

            # Market-hours guard for equities: never even ask Jev about a
            # frozen market, and never place an order while it is closed.
            if not spec.is_24_7:
                try:
                    market_open = alpaca.is_market_open()
                except AlpacaAPIError as exc:
                    api_error_streak += 1
                    print(f"tick {block} | alpaca error checking market hours: {exc}")
                    _sleep_remaining(tick_start, cadence_s)
                    n += 1
                    continue
                if not market_open:
                    record = _closed_market_record(block, now, spec.symbol)
                    _append_log(record)
                    recent_ticks.append(record)
                    if len(recent_ticks) > LATEST_WINDOW:
                        recent_ticks = recent_ticks[-LATEST_WINDOW:]
                    _write_latest(
                        spec.symbol,
                        block,
                        recent_ticks,
                        {"route": None, "model": None},
                        started_at,
                        api_error_streak,
                    )
                    print(
                        f"tick {block} | {spec.symbol} market is closed, prices are stale | HOLD"
                    )
                    n += 1
                    _sleep_remaining(tick_start, cadence_s)
                    continue

            # 2. read the book
            try:
                bids, asks = _read_top_of_book(alpaca, spec)
                if bar_seconds:
                    # Only the flow around this close; the gap since the last
                    # bar can hold more trades than one fetch returns.
                    trades_since_iso = _iso(now - TRADE_TAPE_S)
                recent = alpaca.get_recent_trades(start_iso=trades_since_iso)
                api_error_streak = 0
            except AlpacaAPIError as exc:
                api_error_streak += 1
                print(f"tick {block} | alpaca error: {exc}")
                _sleep_remaining(tick_start, cadence_s)
                n += 1
                continue

            # Real trades only, each with the venue's own timestamp, each
            # counted once (deduplicated on the trade id).
            new_trades = []
            for t in recent:
                tid = t.get("i")
                if tid in seen_trade_ids:
                    continue
                seen_trade_ids.add(tid)
                ts = _parse_ts(t["t"])
                if ts > now:
                    continue  # never let a trade stamped after as_of in
                side = "buy" if t.get("tks") == "B" else "sell"
                new_trades.append((ts, float(t["p"]), float(t["s"]), side))
            if new_trades:
                trades_since_iso = _iso(max(ts for ts, *_ in new_trades))
            trade_tape.extend(new_trades)
            trade_tape = [x for x in trade_tape if x[0] >= now - TRADE_TAPE_S]
            if len(seen_trade_ids) > 5000:
                seen_trade_ids = {t.get("i") for t in recent}
            update_vwap(inv, [(ts, p, sz) for ts, p, sz, _ in new_trades])

            last_trade_px = trade_tape[-1][1] if trade_tape else (price_hist[-1][1] if price_hist else 0.0)
            mid = (
                (bids[0][0] + asks[0][0]) / 2
                if bids and asks and bids[0][0] and asks[0][0]
                else last_trade_px
            )
            microprice = mid  # depth-weighted microprice; mid is a fair stand-in when depth is thin
            spread_bps = (
                (asks[0][0] - bids[0][0]) / mid * 10_000
                if (mid and bids and asks)
                else 0.0
            )

            data_ts = now
            if bar_seconds:
                try:
                    price_hist = _minute_bar_history(alpaca, now)
                except (AlpacaAPIError, KeyError, ValueError) as exc:
                    print(f"tick {block} | minute bars unavailable ({exc}), using the last history")
            if mid > 0:
                price_hist.append((now, mid))
            price_hist = [x for x in price_hist if x[0] >= now - HISTORY_S]
            trade_prices = price_hist
            trade_sides = [(ts, side) for ts, _, _, side in trade_tape]

            # Fills come from Alpaca, not from assumptions: read this run's
            # orders back and fold in only what actually filled, at the price
            # it actually filled at.
            recon_txt = None
            fill_qty = None
            fill_price = None
            if reconcile_needed and not dry_execution:
                try:
                    new_fills, reconcile_needed = reconcile_fills(
                        alpaca, inv, fills_seen, expected_px, session_start_iso, now,
                        open_buys=open_buys,
                    )
                    if not reconcile_needed:
                        open_buys["usd"] = 0.0
                    if new_fills:
                        fill_qty = sum(q for _, q, _ in new_fills)
                        fill_price = sum(q * px for _, q, px in new_fills) / fill_qty
                        recon_txt = ", ".join(
                            f"filled {sd} {q:.8g} @ {px:,.2f}" for sd, q, px in new_fills
                        )
                        fee = sync_fees(inv, alpaca.get_position_qty() - start_held, fill_price)
                        if fee:
                            recon_txt += f", fee ${fee:.4f}"
                except AlpacaAPIError as exc:
                    api_error_streak += 1
                    print(f"tick {block} | could not read fills back: {exc}")

            # 3. state snapshot
            snapshot = build_snapshot(
                as_of=now,
                mid=mid,
                microprice=microprice,
                spread_bps=spread_bps,
                bid_depth=bids,
                ask_depth=asks,
                trade_prices=trade_prices,
                trade_sides=trade_sides,
                inv=inv,
                data_timestamp=data_ts,
                has_depth=spec.has_depth,
            )
            equity_now = inv.equity_usd + inv.realised_pnl_usd + snapshot["unrealised_pnl_usd"]
            inv.high_water_mark_usd = max(inv.high_water_mark_usd, equity_now)

            # 4. battery, throttled. The fast loop never waits on Jev: Jev is
            # called only when the throttle says so, and every other tick
            # reuses the last judgment (see "Jev call rate" in README.md).
            elapsed = time.monotonic() - tick_start
            budget = max(0.05, deadline_s - elapsed - 0.15)
            decision_late = False
            answers = None
            meta = {"model": None, "latency_ms": None, "route": None}
            clock = time.monotonic()
            call_reason = throttle.due(snapshot, clock)
            jev_down = False
            if call_reason is not None:
                throttle.called(clock)
                try:
                    answers, meta = run_battery(client, snapshot, timeout=budget)
                    throttle.success(answers, meta, snapshot, clock)
                except RateLimited as exc:
                    wait = throttle.rate_limited(clock)
                    print(
                        f"tick {block} | Jev rate limit ({exc}), backing off {wait:.0f}s, "
                        f"reusing the last judgment meanwhile"
                    )
                except GatewayVerificationRequired as exc:
                    print(
                        f"tick {block} | gateway needs a card on file: {exc}\n"
                        f"           add one at https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card, "
                        f"falling back to the mock decision client for the rest of this run."
                    )
                    client = resolve_decision_client(mock=True)
                    jev_down = True
                    if not dry_execution:
                        dry_execution = True
                        print(f"tick {block} | mock decisions: dry execution from here, no more orders.")
                except DecisionClientError as exc:
                    msg = str(exc)
                    if "deadline" in msg:
                        decision_late = True
                    else:
                        jev_down = True
                        print(f"tick {block} | decision client error: {exc}")
            if answers is None and not jev_down:
                cached = throttle.last(clock)
                if cached is not None:
                    answers, meta = cached
                    decision_late = False
                elif not decision_late:
                    jev_down = True  # no judgment young enough to reuse: RULES_ONLY

            # 5. policy engine (code) + 6. pricing (code)
            if decision_late:
                action = None
            elif jev_down or answers is None:
                action = fallback_action(snapshot, limits)
            else:
                action = compose_action(answers, snapshot, limits)

            sigma = snapshot["realised_vol_short"] or 0.0005
            bid_px, ask_px = quote_prices(
                mid=mid,
                inventory_frac=snapshot["inventory"] * mid / limits.max_position_usd,
                sigma=sigma,
                gamma=limits.as_gamma,
                kappa=limits.as_kappa,
                horizon_s=limits.as_horizon_s,
                min_half_bps=limits.min_half_spread_bps,
                max_half_bps=limits.max_half_spread_bps,
            )

            # ladder
            decision_conf = None
            if action is not None and answers is not None:
                decision_conf = answers.get("quote_environment", {}).get("confidence")
            execution_health_score = (
                answers["execution_health"]["score"] if answers else None
            )
            risk_kill_pre = snapshot["drawdown_pct"] > limits.max_drawdown_pct
            rung = select_rung(
                risk_kill=risk_kill_pre,
                decision_late=decision_late,
                jev_down=jev_down and not decision_late,
                decision_confidence=decision_conf,
                low_confidence_threshold=limits.low_confidence_threshold,
                execution_health_score=execution_health_score,
            )

            size_factor = limits.reduce_size_factor if rung == Rung.REDUCE else 1.0
            quote_notional = limits.quote_notional_usd * size_factor
            directional_notional = limits.directional_notional_usd * size_factor

            fill_txt = "-"
            kill = False
            seq_before = getattr(alpaca, "_order_seq", 0)
            if rung == Rung.HOLD_LATE or action is None:
                line_action = "HOLD (late)"
            elif rung == Rung.KILL or (action and action.kind == KILL):
                line_action = "KILL (flatten)"
                kill = True
            else:
                # 7. risk veto happens inside execute_action via risk.check
                (
                    line_action,
                    fill_txt,
                    _exec_fill_qty,
                    _exec_fill_price,
                    resting_quotes,
                    rest_counter,
                ) = _execute_action(
                    alpaca=alpaca,
                    spec=spec,
                    action=action,
                    bid_px=bid_px,
                    ask_px=ask_px,
                    mid=mid,
                    quote_notional=quote_notional,
                    directional_notional=directional_notional,
                    snapshot=snapshot,
                    limits=limits,
                    inv=inv,
                    api_error_streak=api_error_streak,
                    decision_latency_ms=meta.get("latency_ms"),
                    resting_quotes=resting_quotes,
                    rest_counter=rest_counter,
                    now=now,
                    dry=dry_execution,
                    expected_px=expected_px,
                    pending_buy_usd=open_buys["usd"],
                )
                if line_action.startswith("KILL"):
                    kill = True  # a hard risk limit tripped inside the risk check
            if getattr(alpaca, "_order_seq", 0) != seq_before:
                reconcile_needed = True
            if kill:
                rung = Rung.KILL
                resting_quotes = None
                fill_txt = _flatten(
                    alpaca, spec, inv, dry_execution, fills_seen, expected_px,
                    session_start_iso, now,
                )
            elif recon_txt:
                fill_txt = recon_txt if fill_txt == "-" else f"{fill_txt}; {recon_txt}"

            # 9. log
            record = {
                "tick": block,
                "ts": now,
                "symbol": spec.symbol,
                "mid": mid,
                "vwap": snapshot["vwap"],
                "spread_bps": round(spread_bps, 2),
                "has_depth": spec.has_depth,
                "regime": answers["regime"]["choice"] if answers else None,
                "regime_conf": answers["regime"]["confidence"] if answers else None,
                "direction": answers["direction"]["choice"] if answers else None,
                "direction_conf": (
                    answers["direction"]["confidence"] if answers else None
                ),
                "toxic_flow": answers["toxic_flow"]["noul"] if answers else None,
                "liquidity_stressed": (
                    answers["liquidity_stressed"]["noul"] if answers else None
                ),
                "quote_environment": (
                    answers["quote_environment"]["score"] if answers else None
                ),
                "quote_environment_conf": (
                    answers["quote_environment"]["confidence"] if answers else None
                ),
                "inventory_pressure": (
                    answers["inventory_pressure"]["score"] if answers else None
                ),
                "execution_health": (
                    answers["execution_health"]["score"] if answers else None
                ),
                "action": action.kind if action else "HOLD_LATE",
                "action_reason": action.reason if action else "block deadline exceeded",
                "direction_leg": action.direction_leg if action else None,
                "skew": action.skew if action else 0.0,
                "rung": rung.value,
                "paused": line_action.startswith("PAUSED"),
                "late": rung == Rung.HOLD_LATE,
                "latency_ms": None if meta.get("reused") else meta.get("latency_ms"),
                "jev_reused": bool(meta.get("reused")),
                "judgment_age_s": meta.get("age_s", 0.0 if answers else None),
                "model": meta.get("model"),
                "route": meta.get("route"),
                "inventory": inv.inventory,
                "unrealised_pnl_usd": snapshot["unrealised_pnl_usd"],
                "drawdown_pct": snapshot["drawdown_pct"],
                "fees_usd": round(inv.fees_usd, 4),
                "fill": fill_txt,
                "fill_qty": fill_qty,
                "fill_price": fill_price,
            }
            _append_log(record)
            recent_ticks.append(record)
            if len(recent_ticks) > LATEST_WINDOW:
                recent_ticks = recent_ticks[-LATEST_WINDOW:]
            _write_latest(
                spec.symbol, block, recent_ticks, meta, started_at, api_error_streak
            )

            regime_txt = (
                f"{answers['regime']['choice']} {answers['regime']['confidence']*100:.0f}%"
                if answers
                else "n/a"
            )
            tox_txt = f"{answers['toxic_flow']['noul']:.2f}" if answers else "n/a"
            env_txt = (
                f"{answers['quote_environment']['score']:.1f}" if answers else "n/a"
            )
            xh_txt = f"{answers['execution_health']['score']:.1f}" if answers else "n/a"
            ms_txt = (
                f"reused {meta.get('age_s', 0):.0f}s" if meta.get("reused")
                else f"{meta.get('latency_ms'):.0f} ms" if meta.get("latency_ms") else "late"
            )
            print(
                f"tick {block} | mid {_fmt_money(mid)} | regime {regime_txt} | "
                f"tox {tox_txt} | env {env_txt} | xh {xh_txt} | {ms_txt} | {line_action} | {fill_txt}"
            )

            if rung == Rung.KILL:
                print(f"tick {block} | KILL: hard limit breached, {fill_txt}, stopping.")
                break

            n += 1
            _sleep_remaining(tick_start, cadence_s)

        return 0
    except (KeyboardInterrupt, _StopRequested):
        print(f"\ntick {block} | stopping: interrupt received.")
        return 0
    finally:
        # Every way out (Ctrl+C, kill, --ticks N finishing, KILL, a crash)
        # cancels the resting orders this run placed. The first published
        # version only did this on Ctrl+C, so a finished run left its
        # quotes working at the broker.
        if getattr(alpaca, "_order_seq", 0) and not dry_execution:
            try:
                n_cancelled = alpaca.cancel_own_orders()
                print(
                    f"tick {block} | {n_cancelled} of this run's resting orders cancelled "
                    "(nothing else on the account touched), shut down cleanly."
                )
            except AlpacaAPIError as exc:
                print(
                    f"tick {block} | could not cancel resting orders cleanly: {exc}. "
                    "Check Open Orders in Alpaca and cancel any starting with 'jevloop-'."
                )
        else:
            print(f"tick {block} | shut down cleanly, no resting orders to cancel.")
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


def _read_top_of_book(
    alpaca, spec: AssetSpec
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Crypto: real L2 depth from the order book. Equities: best bid/ask
    from the latest quote only, wrapped in the same (price, size) shape so
    the rest of the loop never has to know the difference. Returns empty
    lists, never fabricated levels, when nothing is available."""
    if spec.has_depth:
        book = alpaca.get_orderbook()
        bids = [(float(l["p"]), float(l["s"])) for l in book.get("b", [])]
        asks = [(float(l["p"]), float(l["s"])) for l in book.get("a", [])]
        return bids, asks

    quote = alpaca.get_latest_quote()
    bids = (
        [(float(quote["bp"]), float(quote.get("bs", 0.0)))] if quote.get("bp") else []
    )
    asks = (
        [(float(quote["ap"]), float(quote.get("as", 0.0)))] if quote.get("ap") else []
    )
    return bids, asks


def _closed_market_record(block: int, now: float, symbol: str) -> dict:
    return {
        "tick": block,
        "ts": now,
        "symbol": symbol,
        "mid": None,
        "vwap": None,
        "spread_bps": None,
        "has_depth": False,
        "regime": None,
        "regime_conf": None,
        "direction": None,
        "toxic_flow": None,
        "liquidity_stressed": None,
        "quote_environment": None,
        "quote_environment_conf": None,
        "inventory_pressure": None,
        "execution_health": None,
        "action": "MARKET_CLOSED",
        "action_reason": "market closed, prices are stale",
        "direction_leg": None,
        "skew": 0.0,
        "rung": "hold_late",
        "late": False,
        "latency_ms": None,
        "model": None,
        "route": None,
        "inventory": 0.0,
        "unrealised_pnl_usd": 0.0,
        "drawdown_pct": 0.0,
        "fill": "-",
        "fill_qty": None,
        "fill_price": None,
    }


def _execute_action(
    *,
    alpaca,
    spec: AssetSpec,
    action,
    bid_px,
    ask_px,
    mid,
    quote_notional,
    directional_notional,
    snapshot,
    limits,
    inv,
    api_error_streak,
    decision_latency_ms,
    resting_quotes,
    rest_counter,
    now,
    dry: bool = False,
    expected_px: dict | None = None,
    pending_buy_usd: float = 0.0,
):
    """Runs the risk check, then places (or, if `dry` is true, only logs)
    the order implied by `action`. `dry` never calls cancel_own_orders,
    submit_limit_order, or submit_market_order: it reads real market data
    and gets a real Jev battery answer, but never touches the Alpaca order
    book. Every fill line in dry mode starts with "dry:". Order sizes are
    computed from a dollar target (assets.size_order), not a fixed
    quantity, so the same limits work across any asset."""
    from .risk import check as risk_check

    # Held too long: stop adding and close it out. The first published
    # version vetoed every order instead, the exit included, so a stuck
    # position froze the loop after 15 minutes.
    aged = (
        snapshot.get("inventory", 0) != 0
        and snapshot.get("position_age_s", 0) > limits.max_inventory_age_s
    )
    order_notional_usd = max(quote_notional, directional_notional)
    verdict = risk_check(
        snapshot, order_notional_usd, limits, api_error_streak, decision_latency_ms,
        closing=aged,
    )
    if not verdict.ok:
        if verdict.kill:
            return f"KILL ({verdict.veto})", "-", None, None, None, 0
        return (
            f"VETOED ({verdict.veto})",
            "-",
            None,
            None,
            resting_quotes,
            rest_counter,
        )

    # Stop on the dashboard: checked before every order. Pulls this run's
    # resting quotes so nothing new can fill, and places nothing. It never
    # closes a position; a KILL (above, and in the loop) still does.
    if control.is_paused(LOG_DIR):
        fill_txt = "-"
        if resting_quotes and not dry:
            try:
                alpaca.cancel_own_orders()
                fill_txt = "resting quotes pulled"
            except AlpacaAPIError as exc:
                fill_txt = f"could not pull resting quotes: {exc}"
        return "PAUSED (no new orders)", fill_txt, None, None, None, 0

    if aged:
        line_action = "EXIT (held past max_inventory_age_s)"
        if dry:
            return line_action, f"dry: would close {inv.inventory}", None, None, None, 0
        try:
            alpaca.cancel_own_orders()
            fill_txt = _close_position(alpaca, spec, inv, mid, expected_px)
        except MarketClosedError as exc:
            fill_txt = f"exit skipped: {exc}"
        except AlpacaAPIError as exc:
            inv.orders_rejected += 1
            fill_txt = f"exit rejected: {exc}"
        return line_action, fill_txt, None, None, None, 0

    # Room left under the position cap, counting buys still working at the
    # broker, not just what has filled. Without this, resting bids and buy
    # legs piled up past max_position_usd before any of them showed as
    # inventory.
    position_usd = max(snapshot.get("inventory", 0.0), 0.0) * mid
    buy_room_usd = limits.max_position_usd - position_usd - pending_buy_usd

    fill_txt = "-"
    line_action = action.kind

    if action.kind in (PULL_QUOTES, STAND_DOWN):
        if resting_quotes and not dry:
            try:
                alpaca.cancel_own_orders()
            except AlpacaAPIError:
                pass
        return line_action, fill_txt, None, None, None, 0

    if action.kind == WIDEN:
        wide_bid, wide_ask = bid_px * 0.999, ask_px * 1.001
        return (
            f"{line_action} bid {wide_bid:,.1f}/ask {wide_ask:,.1f}",
            fill_txt,
            None,
            None,
            resting_quotes,
            rest_counter,
        )

    if action.kind in (QUOTE_BOTH_SIDES, QUOTE_WIDE):
        fill_qty, fill_price = None, None
        buy_qty = size_order(quote_notional, bid_px, spec)
        sell_qty = size_order(quote_notional, ask_px, spec)
        # Gas-honesty rule: only cancel-replace every `rest_ticks` ticks.
        rest_counter += 1
        if resting_quotes is None or rest_counter >= limits.rest_ticks:
            if dry:
                resting_quotes = {"bid": bid_px, "ask": ask_px}
                rest_counter = 0
                fill_txt = f"dry: would quote {buy_qty}/{sell_qty} @ {bid_px:,.2f}/{ask_px:,.2f}"
            else:
                # Note: this account cannot short. A sell quote placed with
                # no inventory to back it is a real, expected rejection on a
                # cash account, caught as an AlpacaAPIError below.
                try:
                    if resting_quotes:
                        alpaca.cancel_own_orders()
                        # Our resting bid is gone; only filled coin counts now.
                        held_usd = max(alpaca.get_position_qty(), inv.inventory, 0.0) * mid
                        buy_room_usd = limits.max_position_usd - held_usd
                    sides = [("sell", sell_qty, ask_px)]
                    if buy_qty * bid_px <= buy_room_usd:
                        sides.insert(0, ("buy", buy_qty, bid_px))
                        buy_room_usd -= buy_qty * bid_px
                    else:
                        fill_txt = f"bid skipped: ${limits.max_position_usd:.0f} position cap"
                    inv.orders_submitted += len(sides)
                    for side_, qty_, px_ in sides:
                        o = alpaca.submit_limit_order(side_, qty_, px_)
                        if expected_px is not None and o.get("client_order_id"):
                            expected_px[o["client_order_id"]] = px_
                    resting_quotes = {"bid": bid_px, "ask": ask_px}
                    rest_counter = 0
                except MarketClosedError as exc:
                    return (
                        f"{line_action} ({exc})",
                        fill_txt,
                        None,
                        None,
                        resting_quotes,
                        rest_counter,
                    )
                except AlpacaAPIError as exc:
                    return (
                        f"{line_action} (order error: {exc})",
                        fill_txt,
                        None,
                        None,
                        resting_quotes,
                        rest_counter,
                    )

        if action.direction_leg == "down" and not spec.shorting_allowed:
            # Alpaca crypto is spot and this account is cash, so it cannot
            # short. The bot runs long or flat: a SELL closes the long this
            # run built (never more than Alpaca says is held) and does
            # nothing when flat.
            line_action = f"{action.kind} skew {action.skew:+.1f} + sell leg"
            own = max(inv.inventory, 0.0)
            if own <= 0:
                fill_txt = ("dry: " if dry else "") + "no position to close"
            elif dry:
                fill_txt = f"dry: would close long {own}"
                line_action += " (dry)"
            else:
                try:
                    held = max(alpaca.get_position_qty(), 0.0)
                    qty = _floor_qty(min(own, held), spec)
                    if qty <= 0:
                        fill_txt = "no position to close"
                    else:
                        inv.orders_submitted += 1
                        o = alpaca.submit_market_order("sell", qty)
                        if expected_px is not None and o.get("client_order_id"):
                            expected_px[o["client_order_id"]] = mid
                        fill_txt = f"sent market sell {qty} (closing long)"
                except MarketClosedError as exc:
                    fill_txt = f"leg skipped: {exc}"
                except AlpacaAPIError as exc:
                    inv.orders_rejected += 1
                    fill_txt = f"leg rejected: {exc}"
        elif action.direction_leg in ("up", "down"):
            side = "buy" if action.direction_leg == "up" else "sell"
            fill_px = bid_px if side == "sell" else ask_px
            leg_qty = size_order(directional_notional, fill_px, spec)
            if side == "buy" and leg_qty * fill_px > buy_room_usd:
                fill_txt = f"buy leg skipped: ${limits.max_position_usd:.0f} position cap"
                line_action = f"{action.kind} skew {action.skew:+.1f}"
            elif dry:
                fill_txt = f"dry: would {side} {leg_qty} @ {fill_px:,.2f}"
                line_action = (
                    f"{action.kind} skew {action.skew:+.1f} + {side} leg (dry)"
                )
            else:
                try:
                    inv.orders_submitted += 1
                    o = alpaca.submit_market_order(side, leg_qty)
                    if expected_px is not None and o.get("client_order_id"):
                        expected_px[o["client_order_id"]] = mid
                    # Not recorded as filled here: inventory and PnL only move
                    # when Alpaca reports the fill (reconcile_fills, next tick).
                    fill_txt = f"sent market {side} {leg_qty}"
                    line_action = f"{action.kind} skew {action.skew:+.1f} + {side} leg"
                except MarketClosedError as exc:
                    fill_txt = f"leg skipped: {exc}"
                except AlpacaAPIError as exc:
                    inv.orders_rejected += 1
                    fill_txt = f"leg rejected: {exc}"
        else:
            line_action = f"{action.kind} skew {action.skew:+.1f}"

        return line_action, fill_txt, fill_qty, fill_price, resting_quotes, rest_counter

    return line_action, fill_txt, None, None, resting_quotes, rest_counter


def _seconds_to_bar_close(now: float, bar_seconds: float, settle: float = BAR_SETTLE_S) -> float:
    """Seconds from `now` until the next bar close (wall clock, UTC-aligned,
    so 30m bars close at :00 and :30), plus a short settle."""
    next_close = (math.floor((now - settle) / bar_seconds) + 1) * bar_seconds + settle
    return max(0.0, next_close - now)


def _minute_bar_history(alpaca, now: float) -> list[tuple[float, float]]:
    """Real one-minute closes for the last HISTORY_S seconds, each stamped at
    its bar's close. Used at startup, and at every bar close in --bar mode so
    the 1m/5m/30m returns and volatility stay real between decisions."""
    hist: list[tuple[float, float]] = []
    for b in alpaca.get_minute_bars(_iso(now - HISTORY_S)):
        close_ts = _parse_ts(b["t"]) + 60.0
        if close_ts <= now and float(b.get("c", 0)) > 0:
            hist.append((close_ts, float(b["c"])))
    return hist


def _sleep_remaining(tick_start: float, tick_seconds: float) -> None:
    elapsed = time.monotonic() - tick_start
    remaining = tick_seconds - elapsed
    if remaining > 0:
        time.sleep(remaining)


def _append_log(record: dict) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _write_latest(symbol, block, ticks, meta, started_at, api_error_streak) -> None:
    calls = len(ticks)
    late = sum(1 for t in ticks if t["action"] in ("HOLD_LATE", "MARKET_CLOSED"))
    latencies = [t["latency_ms"] for t in ticks if t.get("latency_ms")]
    avg_ms = sum(latencies) / len(latencies) if latencies else None
    payload = {
        "generated_at": time.time(),
        "symbol": symbol,
        "block": block,
        "ticks": ticks,
        "stats": {
            "avg_ms": avg_ms,
            "calls": calls,
            "late_count": late,
            "uptime_s": time.time() - started_at,
            "decision_client": meta.get("route"),
            "model": meta.get("model"),
            "api_error_streak": api_error_streak,
        },
    }
    _atomic_write(LATEST_FILE, json.dumps(payload))


def _atomic_write(path: Path, text: str, attempts: int = 8, first_wait_s: float = 0.01) -> bool:
    """Write to a temp file, then swap it in with os.replace. On Windows the
    swap raises PermissionError while another process (the dashboard server)
    has the old file open, where Mac and Linux allow it. Retry with a short
    backoff (10, 20, 40 ... ms, about 2.5 s in total); if the file is still
    locked, skip this tick's refresh rather than crash the loop. The next
    tick writes a fresh copy anyway."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    wait = first_wait_s
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt == attempts - 1:
                break
            time.sleep(wait)
            wait *= 2
    try:
        tmp.unlink()
    except OSError:
        pass
    print(f"note: {path.name} was locked by a reader, skipped one refresh")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop run")
    parser.add_argument(
        "--paper",
        action="store_true",
        default=True,
        help="paper mode (the default, and the only mode unless --live is used)",
    )
    parser.add_argument(
        "--mock", action="store_true", help="force the mock decision client"
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="stop after N ticks; 0 means run forever (same as --forever)",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="run continuously until stopped (Ctrl+C, or SIGTERM in the background), same as --ticks 0",
    )
    parser.add_argument("--symbol", default=os.environ.get("DEFAULT_SYMBOL", "BTC/USD"))
    parser.add_argument(
        "--dry-execution",
        action="store_true",
        help="real market data and a real Jev battery, but never submit an order to Alpaca",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "trade real money on Alpaca's live endpoint instead of paper. Also "
            "requires JEV_LOOP_ALLOW_LIVE=i-understand-the-risk in the environment "
            "and a typed confirmation at startup; missing either refuses to start. "
            "See SKILL.md / README.md before ever using this."
        ),
    )
    parser.add_argument(
        "--bar",
        choices=sorted(BAR_CHOICES, key=BAR_CHOICES.get),
        default=None,
        help=(
            "decide once per bar close (e.g. 30m = at :00 and :30 UTC) instead of "
            "every 2 s block. Returns and volatility come from real one-minute bars."
        ),
    )
    args = parser.parse_args(argv)

    ticks = None if (args.forever or args.ticks == 0) else args.ticks

    confirmation = None
    if args.live:
        print("\n" + "!" * 70)
        print("! --live was passed. This is not paper trading.")
        print("! Every order this places spends real money in your real Alpaca")
        print("! account, with no paper safety net.")
        print("!" * 70)
        try:
            confirmation = input(
                f"Type exactly '{LIVE_CONFIRMATION_PHRASE}' to continue, anything else cancels: "
            )
        except EOFError:
            confirmation = None

    limits = Limits()
    return run(
        symbol=args.symbol,
        ticks=ticks,
        mock=args.mock,
        limits=limits,
        dry_execution=args.dry_execution,
        live=args.live,
        confirmation=confirmation,
        bar_seconds=BAR_CHOICES[args.bar] if args.bar else None,
    )


if __name__ == "__main__":
    sys.exit(main())
