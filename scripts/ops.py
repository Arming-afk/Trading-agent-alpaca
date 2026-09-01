#!/usr/bin/env python3
"""Operator commands for the account, for the cases automation should not own.

    python3 scripts/ops.py report              # what is on, and what is working
    python3 scripts/ops.py cancel-open-orders  # flatten the order book
    python3 scripts/ops.py trim                # cut positions back to approved size

This exists because of a specific incident. On 2026-09-01 the fill chase
cancelled six orders through `alpaca order cancel <id>`, which the CLI rejects
— the id has to arrive as `--order-id`. Every cancel failed, the chase sent a
replacement after each one anyway, and the account was left carrying up to
three live orders for every one intent. The interlock in `agent/execution.py`
stops that class of bug from recurring; this script is how a human cleans up
after one that already has.

Nothing here decides anything. `report` reads, and `cancel-open-orders` cancels
working orders and touches no position — an open order is an intention that has
not happened yet, so withdrawing it costs nothing, while closing a position is
a trade and is not an operator's default.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import cli, config, execution, journal
from agent import positions as pos_mod

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: Order states that are still live and can still fill.
WORKING = {"new", "accepted", "pending_new", "accepted_for_bidding",
           "partially_filled", "held", "replaced", "pending_replace",
           "calculated", "suspended"}


def working_orders() -> list[dict]:
    """Every order that can still trade.

    Asks for open orders and then filters by status anyway. `--status open` is
    the broker's opinion of what open means; the list of states that can still
    put a position on is this program's, and a mismatch between the two is
    exactly the kind of thing worth not depending on during a cleanup.
    """
    try:
        rows = cli.orders(status="open", limit=500)
    except cli.AlpacaCLIError as exc:
        print(f"! could not list orders: {exc}", file=sys.stderr)
        return []
    return [o for o in rows if str(o.get("status") or "").lower() in WORKING]


def describe(order: dict) -> str:
    legs = order.get("legs") or []
    symbols = " / ".join(str(leg.get("symbol") or "") for leg in legs) or \
        str(order.get("symbol") or "")
    return (f"{order.get('id')}  {order.get('order_class') or 'simple':<6} "
            f"qty {order.get('qty'):<4} @ {order.get('limit_price') or 'mkt':<7} "
            f"{order.get('status'):<16} {symbols}")


#: Concessions to walk through when trimming, as a percentage of the mark.
#: Getting out of a position that should not exist is worth more than the last
#: few cents of it, so this starts where a normal close ends.
TRIM_CONCESSIONS = (6.0, 12.0, 25.0)
#: Seconds to leave each trim order resting before improving it.
TRIM_WAIT = 45


def cmd_report() -> int:
    account = cli.account()
    equity = float(account.get("equity") or 0)
    legs = cli.positions()
    spreads, unexplained = pos_mod.reconcile(legs, journal.read(config.DECISIONS_LOG))

    print()
    print(f"  equity ${equity:,.2f}   cash ${float(account.get('cash') or 0):,.2f}")
    print(f"  {len(legs)} option leg(s) -> {len(spreads)} spread(s), "
          f"${pos_mod.open_risk(spreads):,.0f} at risk "
          f"({pos_mod.open_risk(spreads) / equity:.1%} of equity)" if equity else "")
    print()
    for spread in spreads:
        print(f"    {spread.describe()}")
        print(f"      max loss ${spread.max_loss:,.0f}  "
              f"P&L ${spread.unrealized_pl:+,.0f}")
    if unexplained:
        print()
        print("  ! positions the journal cannot explain:")
        for item in unexplained:
            print(f"      {item.describe()}")

    oversize = [s for s in spreads if s.excess_qty > 0]
    if oversize:
        print()
        print("  ! positions larger than the risk gate approved:")
        for item in oversize:
            print(f"      {item.underlying} {item.kind}: {item.qty} on, "
                  f"{item.approved_qty} approved, {item.excess_qty} excess "
                  f"(${item.max_loss:,.0f} at risk, "
                  f"{item.max_loss / equity:.1%} of equity)" if equity else "")
        print("    run `ops.py trim` to cut them back")

    live = working_orders()
    print()
    print(f"  {len(live)} working order(s)")
    for order in live:
        print(f"    {describe(order)}")
    print()
    return 0


def _oversize() -> list:
    legs = cli.positions()
    spreads, _ = pos_mod.reconcile(legs, journal.read(config.DECISIONS_LOG))
    return [s for s in spreads if s.excess_qty > 0]


def _trim_one(spread, *, dry_run: bool) -> bool:
    """Close `spread.excess_qty` contracts and leave the approved size on.

    Trimming rather than flattening is the whole point. The approved position
    is a decision the gates made and there is no reason to unwind it; what has
    to go is the part no gate ever sized. So the closing order carries the
    excess quantity, not the whole position.
    """
    excess = spread.excess_qty
    legs = spread.closing_legs()
    print(f"  {spread.underlying} {spread.kind}: {spread.qty} on, "
          f"{spread.approved_qty} approved -> closing {excess}")

    if len(legs) < 2:
        print("    ! not a closable package; skipping", file=sys.stderr)
        return False
    if dry_run:
        print("    --dry-run: nothing submitted")
        return True

    order_id = None
    for attempt, concession in enumerate(TRIM_CONCESSIONS, start=1):
        if order_id is not None and not execution._cancel(order_id):
            print("    ! could not cancel the resting order — stopping rather "
                  "than stacking a second one", file=sys.stderr)
            return False

        limit = execution.close_price(spread, concession_pct=concession)
        if limit is None:
            print("    ! no mark on a leg; cannot price the exit", file=sys.stderr)
            return False

        try:
            order = cli.submit_mleg(legs, qty=excess, limit_price=limit)
        except cli.AlpacaCLIError as exc:
            print(f"    ! rejected: {exc}", file=sys.stderr)
            return False

        order_id = str(order.get("id") or "")
        print(f"    attempt {attempt}: {excess} @ {limit:.2f} ({concession:.0f}% "
              f"concession) order {order_id}")
        time.sleep(TRIM_WAIT)

        status, filled = execution.poll(order_id)
        if status == "filled" or filled >= excess:
            print(f"    filled {filled} at {limit:.2f}")
            journal.log_decision(
                symbol=spread.underlying, action="closed", regime={},
                spread=spread.as_log(), order=order,
                reason=f"trimmed {excess} contract(s) back to the approved "
                       f"{spread.approved_qty} after an execution incident",
                command=cli.as_argv("order", "submit", "--order-class", "mleg",
                                    "--type", "limit", "--qty", str(excess),
                                    "--limit-price", f"{limit:.2f}",
                                    "--time-in-force", "day", "--legs", "<legs.json>"))
            return True
        print(f"    not filled ({status}); improving")

    print("    ! still not filled after every concession — order left resting",
          file=sys.stderr)
    return False


def cmd_trim(dry_run: bool) -> int:
    oversize = _oversize()
    if not oversize:
        print("  every position is within its approved size")
        return 0

    print(f"  {len(oversize)} oversized position(s)")
    print()
    for spread in oversize:
        _trim_one(spread, dry_run=dry_run)
        print()

    if dry_run:
        return 0

    remaining = _oversize()
    if remaining:
        print("  ! still oversized:", file=sys.stderr)
        for spread in remaining:
            print(f"      {spread.underlying}: {spread.qty} on, "
                  f"{spread.approved_qty} approved", file=sys.stderr)
        return 1
    print("  every position is now within its approved size")
    return 0


def cmd_cancel_open_orders(dry_run: bool) -> int:
    live = working_orders()
    if not live:
        print("  no working orders")
        return 0

    print(f"  {len(live)} working order(s):")
    for order in live:
        print(f"    {describe(order)}")
    print()

    if dry_run:
        print("  --dry-run: nothing cancelled")
        return 0

    failed = []
    for order in live:
        order_id = str(order.get("id") or "")
        try:
            cli.cancel_order(order_id)
            print(f"  cancelled {order_id}")
        except cli.AlpacaCLIError as exc:
            print(f"  ! {order_id} did not cancel: {exc}", file=sys.stderr)
            failed.append(order_id)

    remaining = working_orders()
    print()
    print(f"  {len(remaining)} order(s) still working")
    for order in remaining:
        print(f"    {describe(order)}")

    # A cleanup that reports success while orders are still live is worse than
    # one that fails loudly, because the next thing anyone does is trust it.
    if remaining:
        print("\n  ! the order book is not clear", file=sys.stderr)
        return 1
    if failed:
        print("\n  cancels errored but no order remains working — "
              "they had already gone away")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command",
                        choices=["report", "cancel-open-orders", "trim"])
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be cancelled and stop")
    args = parser.parse_args(argv)

    if args.command == "report":
        return cmd_report()
    if args.command == "trim":
        return cmd_trim(args.dry_run)
    return cmd_cancel_open_orders(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
