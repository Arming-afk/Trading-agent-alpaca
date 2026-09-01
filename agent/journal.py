"""Append-only decision and run records.

These files are the audit trail. They are committed by the daily workflow on
purpose — the competition is judged on P&L in a paper account, and a record that
only exists inside the broker's UI cannot show *why* a position was opened. Two
rules keep them honest:

* **Every considered symbol is written, not only the traded ones.** A log that
  records three trades out of eight candidates and drops the five refusals reads
  as a strategy that fired three times, when it actually declined five times for
  stated reasons. The refusals are the larger part of this strategy's behaviour.

* **What the agent decided and what the broker did are separate fields.** The
  proposed size and the submitted size are both recorded even when they agree,
  so a later mismatch is visible rather than reconstructed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import config


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read(path: Path) -> list[dict]:
    """Every well-formed record in a JSONL file; malformed lines are skipped
    rather than aborting a run that is otherwise fine."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_decision(*, symbol: str, action: str, regime: dict,
                 spread: dict | None = None, risk: dict | None = None,
                 order: dict | None = None, command: list[str] | None = None,
                 reason: str = "", notes: list[str] | None = None) -> dict:
    """One symbol's outcome for one run.

    `action` is what happened: "opened", "closed", "declined" or "error".
    `command` is the argv a human could re-run to reproduce the order.
    """
    record = {
        "timestamp": now_iso(),
        "symbol": symbol,
        "action": action,
        "reason": reason,
        "regime": regime,
        "spread": spread,
        "risk": risk,
        "order": order,
        "command": command,
        "notes": notes or [],
    }
    _append(config.DECISIONS_LOG, record)
    return record


def log_run(record: dict) -> dict:
    record = {"timestamp": now_iso(), **record}
    _append(config.RUNS_LOG, record)
    return record


def equity_history() -> list[float]:
    """Closing equity of every completed run, oldest first — the input to the
    drawdown breaker's high-water mark."""
    out = []
    for row in read(config.RUNS_LOG):
        value = row.get("equity_after")
        if isinstance(value, (int, float)) and value > 0:
            out.append(float(value))
    return out


def trades_opened_today(today: str) -> int:
    """Positions opened in today's UTC date, across all runs today."""
    return sum(
        1 for row in read(config.DECISIONS_LOG)
        if row.get("action") == "opened"
        and str(row.get("timestamp", "")).startswith(today)
    )


def spread_snapshot(spread: Any) -> dict:
    """The parts of a Vertical worth keeping in the record."""
    return {
        "kind": spread.kind,
        "underlying": spread.long_leg.underlying,
        "expiration": str(spread.long_leg.expiration),
        "long_leg": spread.long_leg.symbol,
        "short_leg": spread.short_leg.symbol,
        "long_strike": spread.long_leg.strike,
        "short_strike": spread.short_leg.strike,
        "qty": spread.qty,
        "width": spread.width,
        "net_mid": round(spread.net_mid, 4),
        "max_loss": round(spread.max_loss, 2),
        "max_gain": round(spread.max_gain, 2),
        "breakeven": round(spread.breakeven, 2),
        "reward_risk": round(spread.reward_risk, 4),
        "worst_leg_spread_pct": round(spread.worst_spread_pct, 2),
    }


#: Run records that count as "the agent already did its full pass today".
COMPLETED_STATUSES = ("traded", "dry_run", "no_trades")


def runs_today(today: str) -> list[dict]:
    """Every run record written on `today` (a YYYY-MM-DD string)."""
    return [row for row in read(config.RUNS_LOG)
            if str(row.get("date") or "") == today]


def completed_full_run_today(today: str) -> bool:
    """Whether a full survey has already run today.

    This is what makes a frequent schedule safe. GitHub's cron is best-effort —
    on 2026-08-31 the 14:00 UTC trigger arrived at 19:43, seventeen minutes
    before the close, and on 2026-09-01 it did not arrive at all. The fix is to
    schedule many attempts and let the first one that lands do the work, which
    only holds together if the later ones can tell that it did.

    Note what this does *not* gate: managing open positions and chasing unfilled
    orders still run on every pass. Those are the jobs that benefit from being
    done twelve times a day.
    """
    return any(str(row.get("status") or "") in COMPLETED_STATUSES
               and row.get("mode", "full") == "full"
               for row in runs_today(today))


def last_run(today: str | None = None) -> dict | None:
    """The most recent run record, optionally restricted to one date."""
    rows = runs_today(today) if today else read(config.RUNS_LOG)
    return rows[-1] if rows else None
