"""Event risk: refusing to hold a defined-risk spread across an earnings print.

Why this module exists. Both halves of the strategy break on an earnings date,
in opposite directions:

* **Selling premium into a print** is the trade that wins for months and then
  returns every dollar in one session. A vertical caps the loss, but it caps it
  at the full width — and the whole width is exactly what an earnings gap
  collects.
* **Buying premium after a print** is the mirror-image trap, and it is the one
  that actually fired here. Implied vol collapses the morning after the event
  while trailing realized vol still carries the gap, so IV/RV reads low and the
  agent concludes options are cheap. They are not cheap; the denominator is
  stale. See `vol.is_jump_contaminated`, which catches this case from the price
  series alone.

Two independent defences, because they fail in different places:

1. **The calendar** (this module) is authoritative when a date is on file, and
   knows about events that have not happened yet — which no price series can.
2. **The jump filter** (`agent/vol.py`) needs no calendar and catches the event
   after the fact, including for symbols nobody remembered to maintain.

The calendar ships **empty on purpose**. There is no earnings endpoint in the
Alpaca CLI, and a file of plausible-looking guessed dates is worse than no file:
it would silently authorise trades on the strength of numbers nobody checked.
A symbol with no entry is reported as unknown, not as clear, and the decision
log says which of the two it was.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from agent import config

logger = logging.getLogger(__name__)

#: Curated earnings dates, keyed by symbol. See data/earnings.json.
CALENDAR_PATH = config.ROOT / "data" / "earnings.json"

#: Do not open a new position this many days before a known print, even when
#: the expiry itself is clear — implied vol runs up into the event and a debit
#: bought here is paying for the run-up.
BLACKOUT_DAYS_BEFORE = 2

CLEAR = "clear"
BLOCKED = "blocked"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class EventCheck:
    """Whether a symbol may be traded to a given expiry, and why."""
    symbol: str
    status: str                 # CLEAR | BLOCKED | UNKNOWN
    reason: str
    next_earnings: date | None = None

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    def as_log(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "next_earnings": str(self.next_earnings) if self.next_earnings else None,
        }


def load_calendar(path: Path | None = None) -> dict[str, list[date]]:
    """Symbol → sorted earnings dates. A missing or malformed file yields an
    empty calendar rather than raising: the jump filter still applies, and a
    broken data file must not be able to stop the agent from running."""
    src = path or CALENDAR_PATH
    if not src.exists():
        return {}
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("earnings calendar unreadable (%s) — treating as empty", exc)
        return {}

    events = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(events, dict):
        return {}

    out: dict[str, list[date]] = {}
    for symbol, dates in events.items():
        parsed = []
        for value in dates or []:
            try:
                parsed.append(datetime.strptime(str(value), "%Y-%m-%d").date())
            except ValueError:
                logger.warning("earnings calendar: bad date %r for %s", value, symbol)
        if parsed:
            out[symbol.upper()] = sorted(parsed)
    return out


def next_event(symbol: str, today: date,
               calendar: dict[str, list[date]] | None = None) -> date | None:
    """The next earnings date on or after `today`, if one is on file."""
    cal = load_calendar() if calendar is None else calendar
    upcoming = [d for d in cal.get(symbol.upper(), []) if d >= today]
    return upcoming[0] if upcoming else None


def check(symbol: str, *, today: date, expiration: date | None = None,
          calendar: dict[str, list[date]] | None = None,
          blackout_days: int = BLACKOUT_DAYS_BEFORE) -> EventCheck:
    """May this symbol be traded to `expiration`?

    Blocked when a known print falls inside the holding period, or within
    `blackout_days` of today. Unknown when the symbol has no calendar entry —
    which the caller must not read as clear.
    """
    cal = load_calendar() if calendar is None else calendar
    if symbol.upper() not in cal:
        return EventCheck(symbol, UNKNOWN,
                          "no earnings date on file — relying on the jump filter alone")

    event = next_event(symbol, today, calendar=cal)
    if event is None:
        return EventCheck(symbol, CLEAR, "no earnings date on or after today")

    if (event - today).days <= blackout_days:
        return EventCheck(symbol, BLOCKED,
                          f"earnings {event} is within {blackout_days} days", event)

    if expiration is not None and today <= event <= expiration:
        return EventCheck(symbol, BLOCKED,
                          f"earnings {event} falls before expiry {expiration}", event)

    return EventCheck(symbol, CLEAR, f"next earnings {event} is clear of the holding period", event)


def expiry_is_clear(symbol: str, expiration: date, today: date,
                    calendar: dict[str, list[date]] | None = None) -> bool:
    """Convenience predicate for callers that only need the yes/no."""
    return not check(symbol, today=today, expiration=expiration,
                     calendar=calendar).blocked


def horizon_end(today: date, max_dte: int | None = None) -> date:
    """The furthest expiry the agent could select — what the calendar has to
    cover to be useful."""
    return today + timedelta(days=max_dte if max_dte is not None else config.MAX_DTE)
