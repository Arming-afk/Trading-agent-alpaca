#!/usr/bin/env python3
"""Live watch on orders, fills and open spreads.

Written for the first real submission: the dry run proves the request body is
right, but it cannot show whether a multi-leg limit actually fills, or how far
from the mid it has to sit before it does. That only shows up against a live
book, and it is worth watching the first one rather than reading about it
afterwards.

    python3 scripts/watch.py                 # poll until interrupted
    python3 scripts/watch.py --once          # single snapshot
    python3 scripts/watch.py --interval 10   # seconds between polls
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import chain as ch
from agent import cli

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"

#: Order states that will not change again without us doing something.
TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def money(value: float, *, colour: bool = True) -> str:
    text = f"${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"
    if not colour:
        return text
    return f"{GREEN if value > 0 else RED if value < 0 else ''}{text}{RESET}"


def status_colour(status: str) -> str:
    if status == "filled":
        return GREEN
    if status in ("rejected", "canceled", "expired"):
        return RED
    return YELLOW


def describe_leg(symbol: str) -> str:
    """Render an OCC symbol as something a human reads at a glance."""
    try:
        underlying, expiration, kind, strike = ch.parse_occ(symbol)
    except ValueError:
        return symbol
    return f"{underlying} {expiration:%d %b} {strike:g}{kind[0].upper()}"


def show_orders(limit: int = 20) -> list[dict]:
    orders = cli.run("order", "list", "--status", "all", "--limit", str(limit))
    orders = orders if isinstance(orders, list) else []
    if not orders:
        print(f"{DIM}  no orders yet{RESET}")
        return orders

    for o in orders:
        status = o.get("status", "?")
        legs = o.get("legs") or []
        submitted = str(o.get("submitted_at", ""))[11:19]
        head = (f"  {status_colour(status)}{status:<12}{RESET} "
                f"{DIM}{submitted}{RESET} "
                f"{o.get('order_class','')} qty={o.get('qty')} "
                f"limit={o.get('limit_price')}")
        print(head)

        # A multi-leg order carries its legs; a single-leg one is its own leg.
        for leg in (legs or [o]):
            filled = leg.get("filled_qty") or "0"
            avg = leg.get("filled_avg_price")
            price = f" @ {avg}" if avg else ""
            print(f"      {leg.get('side','?'):<4} {describe_leg(leg.get('symbol',''))}"
                  f"  filled {filled}/{leg.get('qty','?')}{price}")
    return orders


def show_positions() -> float:
    positions = cli.positions()
    if not positions:
        print(f"{DIM}  flat{RESET}")
        return 0.0

    total = 0.0
    today = date.today()
    for p in positions:
        symbol = p.get("symbol", "")
        pl = float(p.get("unrealized_pl") or 0)
        plpc = float(p.get("unrealized_plpc") or 0)
        total += pl
        try:
            _, expiration, _, _ = ch.parse_occ(symbol)
            dte = f"{(expiration - today).days}d"
        except ValueError:
            dte = "—"
        print(f"  {describe_leg(symbol):<22} qty={p.get('qty'):>5}  "
              f"{dte:>4}  mv={float(p.get('market_value') or 0):>10,.2f}  "
              f"{money(pl)} ({plpc:+.1%})")
    return total


def snapshot() -> bool:
    """One pass. Returns True when every order has reached a terminal state."""
    now = datetime.now().strftime("%H:%M:%S")
    try:
        account = cli.account()
        clock = cli.clock()
    except cli.AlpacaCLIError as exc:
        print(f"{RED}  cannot reach Alpaca: {exc}{RESET}")
        return False

    equity = float(account.get("equity") or 0)
    last = float(account.get("last_equity") or equity)
    day_pl = equity - last
    market = f"{GREEN}open{RESET}" if clock.get("is_open") else f"{DIM}closed{RESET}"

    print(f"\n{BOLD}{'─' * 72}{RESET}")
    print(f"{BOLD}{now}{RESET}  market {market}   "
          f"equity {BOLD}${equity:,.2f}{RESET}   day {money(day_pl)}   "
          f"cash ${float(account.get('cash') or 0):,.2f}")

    print(f"\n{CYAN}orders{RESET}")
    orders = show_orders()

    print(f"\n{CYAN}positions{RESET}")
    unrealized = show_positions()
    if unrealized:
        print(f"  {BOLD}unrealized {money(unrealized)}{RESET}")

    return bool(orders) and all(o.get("status") in TERMINAL for o in orders)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Watch orders and positions live.")
    ap.add_argument("--interval", type=int, default=15, help="seconds between polls")
    ap.add_argument("--once", action="store_true", help="one snapshot, then exit")
    ap.add_argument("--until-settled", action="store_true",
                    help="exit once every order reaches a terminal state")
    args = ap.parse_args(argv)

    try:
        while True:
            settled = snapshot()
            if args.once or (args.until_settled and settled):
                if settled and args.until_settled:
                    print(f"\n{GREEN}all orders settled{RESET}")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{RESET}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
