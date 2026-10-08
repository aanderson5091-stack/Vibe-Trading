"""CLI dispatcher: `jev-loop run|calibrate|serve|pause|resume|validate-symbol|explain-split`.

Also invocable as `uv run python -m jevloop <command> ...` from inside the
skill directory, which is what the /jev-loop skill's SKILL.md tells Claude
Code to run.
"""

from __future__ import annotations

import sys

USAGE = "usage: jev-loop <run|backtest|calibrate|serve|pause|resume|validate-symbol|explain-split> [options]"


def _validate_symbol(argv: list[str]) -> int:
    from .assets import UnknownSymbolError, resolve_symbol

    if not argv:
        print("usage: jev-loop validate-symbol <SYMBOL>")
        return 1
    try:
        spec = resolve_symbol(argv[0])
    except UnknownSymbolError as exc:
        print(str(exc))
        return 1

    session = "24/7 (crypto)" if spec.is_24_7 else "market hours only (US equity)"
    print(f"resolved: {spec.symbol}")
    print(f"  asset class:       {spec.asset_class}")
    print(f"  session:           {session}")
    print(f"  min order notional: ${spec.min_notional_usd:.2f}")
    print(f"  quantity precision: {spec.qty_precision} decimal places")
    print(f"  shorting allowed:   {spec.shorting_allowed}")
    print(f"  order book depth:   {'yes' if spec.has_depth else 'best bid/ask only'}")
    return 0


def _explain_split(argv: list[str]) -> int:
    from .split import render_split_table

    print(render_split_table())
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE)
        return 1

    command, rest = sys.argv[1], sys.argv[2:]
    if command == "run":
        from . import loop

        return loop.main(rest)
    if command == "backtest":
        from . import backtest

        return backtest.main(rest)
    if command == "calibrate":
        from . import calibrate

        return calibrate.main(rest)
    if command == "serve":
        from . import serve

        return serve.main(rest)
    if command in ("pause", "resume"):
        from . import control
        from .loop import LOG_DIR

        state = control.PAUSED if command == "pause" else control.RUNNING
        control.write_state(LOG_DIR, state)
        print(f"control: {state}" + (" (no new orders; positions stay open)" if state == control.PAUSED else ""))
        return 0
    if command == "validate-symbol":
        return _validate_symbol(rest)
    if command == "explain-split":
        return _explain_split(rest)

    print(f"unknown command: {command!r}. {USAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
