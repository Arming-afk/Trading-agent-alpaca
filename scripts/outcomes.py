#!/usr/bin/env python3
"""What actually happened to every trade the agent submitted.

The decision log answers "why did it do that". This answers "and then what",
which is the half the record was missing: every entry carried its IV, RV and
ratio, and nothing ever came back to say whether the reading was right.

    python3 scripts/outcomes.py            # live: joins the broker's marks
    python3 scripts/outcomes.py --offline  # journal only, no network

Writes `logs/outcomes.jsonl` — a derived view, regenerated in full each time so
it can never drift from the decision log it is computed from.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import cli, config, journal, outcomes

# A Windows console defaults to cp1252 and cannot encode the characters this
# report prints. Reporting must never be the thing that crashes: degrade the
# glyphs, not the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_STATUS_MARK = {
    outcomes.OPEN: "open",
    outcomes.CLOSED: "closed",
    outcomes.UNFILLED: "NEVER FILLED",
    outcomes.UNRESOLVED: "unresolved",
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--offline", action="store_true",
                   help="use the journal alone; do not call the broker")
    p.add_argument("--json", action="store_true",
                   help="print the summary as JSON instead of a table")
    return p.parse_args(argv)


def _broker_state() -> tuple[list[dict], dict[str, dict]]:
    """Open legs, and every order the agent has a record of, keyed by id.

    Fetching orders is what separates "never filled" from "closed without a
    record" — two very different facts that look identical from the journal.
    """
    try:
        positions = cli.positions()
    except (cli.AlpacaCLIError, cli.AlpacaCLIMissing) as exc:
        print(f"! broker positions unavailable ({exc}) — falling back to offline",
              file=sys.stderr)
        return [], {}

    orders: dict[str, dict] = {}
    for status in ("all",):
        try:
            for row in cli.orders(status=status, limit=500):
                if row.get("id"):
                    orders[str(row["id"])] = row
        except cli.AlpacaCLIError as exc:
            print(f"! order history unavailable ({exc})", file=sys.stderr)
    return positions, orders


def _table(rows: list[outcomes.Outcome]) -> str:
    if not rows:
        return "  (no positions have been submitted yet)"
    head = (f"  {'SYMBOL':<7}{'STRUCTURE':<19}{'IV/RV':>7}"
            f"{'RISK':>10}{'P&L':>10}{'ON RISK':>9}  STATUS")
    lines = [head, "  " + "-" * (len(head) - 2)]
    for o in rows:
        ratio = f"{o.ratio:.2f}" if o.ratio is not None else "-"
        on_risk = f"{o.pl_vs_risk:+.1%}" if o.pl_vs_risk is not None else "-"
        pl = f"{o.pl:+,.0f}" if o.resolved else "-"
        lines.append(
            f"  {o.symbol:<7}{o.kind:<19}{ratio:>7}"
            f"{o.max_loss:>10,.0f}{pl:>10}{on_risk:>9}  "
            f"{_STATUS_MARK.get(o.status, o.status)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    decisions = journal.read(config.DECISIONS_LOG)

    positions, orders = ([], {}) if args.offline else _broker_state()
    rows = outcomes.build(decisions, positions, orders)
    summary = outcomes.summarise(rows)

    # An offline run must not overwrite the committed report. It produces a
    # strictly poorer version of the same file — every row unresolved, because
    # it could not ask the account — and writing that over a report built from
    # live marks replaces answers with the absence of answers, in a file that
    # then gets committed.
    if summary["broker_data"]:
        outcomes.write(rows, summary)
    else:
        print(f"! offline: leaving {config.LOGS / 'outcomes.jsonl'} untouched",
              file=sys.stderr)

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print()
    print("  TRADE OUTCOMES" + ("  (offline - marks not included)" if args.offline else ""))
    print()
    print(_table(rows))
    print()

    if not summary["broker_data"]:
        # Every count below depends on the account. Printing them as zeros
        # would state as fact the one thing this run could not determine.
        print(f"  Submitted {summary['trades_submitted']}. Fills, P&L and "
              f"outcomes are unknown without the broker.")
        print()
    else:
        fill_rate = summary["fill_rate"]
        print(f"  Submitted {summary['trades_submitted']}, filled {summary['filled']}"
              + (f", never filled {summary['unfilled']}" if summary["unfilled"] else "")
              + (f"  ({fill_rate:.0%} fill rate)" if fill_rate is not None else ""))
        print(f"  Total P&L on filled positions: ${summary['total_pl']:,.2f}")
        print()

        for label, bucket in summary["by_stance"].items():
            ror = bucket["return_on_risk"]
            ror_text = f"{ror:+.1%} on risk" if ror is not None else "no resolved risk"
            print(f"  {label:<14} {bucket['resolved']} resolved of {bucket['trades']}"
                  f"  ·  ${bucket['pl']:+,.0f}  ·  {ror_text}")
        print()

    stale = outcomes.expired_today(rows, date.today())
    if stale:
        print("  ! expired without a closing record — reconcile by hand:")
        for o in stale:
            print(f"      {o.symbol} {o.kind} exp {o.expiration}")
        print()

    print("  THE FALSIFICATION TEST" if summary["broker_data"] else "  WHAT THIS RUN CAN SAY")
    for line in _wrap(summary["verdict"], 74):
        print(f"  {line}")
    print()
    if summary["broker_data"]:
        print(f"  written to {(config.LOGS / 'outcomes.jsonl')}")
        print()
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
