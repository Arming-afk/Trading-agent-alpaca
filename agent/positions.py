"""Reconciling what the broker holds back into the spreads the agent opened.

Alpaca reports option positions **leg by leg**. It has no concept of the
vertical the two legs belong to — that structure exists only in this agent's
own decision log. Two things that matter both broke on that gap:

**1. Portfolio risk was arithmetic on the wrong number.** The runner summed
`abs(cost_basis)` across legs and called it open risk. For a debit spread that
is roughly right by accident. For a credit spread it is unrelated to the
answer: the position's worst case is `width − credit`, while the cost basis is
the small net credit itself. A $1,867 risk was being reported to the 25%
portfolio gate as a few hundred dollars of the wrong sign.

**2. Profit-taking was evaluated per leg.** A 60% profit target applied to a
leg is not the spread's profit target. On a bull put credit spread the short
leg reaches +60% of *its own* cost long before the package reaches 60% of max
profit — and closing that leg alone leaves the long put stranded: the agent
would have paid to keep the worthless half of its own hedge.

So the journal is the authority on structure and the broker is the authority on
what is still open. This module joins them:

* a **matched** spread has both journal-recorded legs still open at the broker,
  and therefore a known `max_loss` recorded at entry;
* a **partial** has one leg left — either a fill that only half-worked or a
  close that only half-worked — and it is reported, not silently averaged in;
* an **orphan** is a leg at the broker that no journal record explains, which
  is the shape a manual intervention leaves behind.

Nothing here places an order. It answers "what is on, and what is it worth".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from agent import chain as ch
from agent import config, journal

logger = logging.getLogger(__name__)

MATCHED = "matched"
PARTIAL = "partial"
ORPHAN = "orphan"


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class OpenSpread:
    """One reconstructed position: the structure from the journal, the money
    from the broker."""
    underlying: str
    kind: str
    expiration: date | None
    long_symbol: str | None
    short_symbol: str | None
    #: Contracts actually on at the broker.
    qty: int
    #: Worst case in dollars, as computed and logged at entry.
    max_loss: float
    #: Best case in dollars, as computed and logged at entry.
    max_gain: float
    legs: list[dict] = field(default_factory=list)
    state: str = MATCHED
    entry: dict | None = None
    #: Contracts the risk gate approved, as recorded in the journal at entry.
    #: Normally equal to `qty`; a difference means the account is carrying a
    #: position no gate ever sized.
    approved_qty: int = 0

    @property
    def excess_qty(self) -> int:
        """Contracts on beyond what was approved.

        This is not a theoretical field. On 2026-09-01 a failed cancel let the
        fill chase submit three orders for one intent, all three filled, and
        the account carried six SPY spreads against an approved two and twelve
        AAPL against four — 4.0% and 5.0% of equity each, against a 2% per-trade
        cap. The broker reports a position; only the journal knows how large it
        was supposed to be, so only the join can see the breach at all.
        """
        if self.state != MATCHED or self.approved_qty <= 0:
            return 0
        return max(self.qty - self.approved_qty, 0)

    @property
    def symbols(self) -> list[str]:
        return [p.get("symbol", "") for p in self.legs]

    @property
    def unrealized_pl(self) -> float:
        """Package P&L: the sum across legs. A spread's profit is not any one
        leg's profit, and neither leg's percentage means anything alone."""
        return sum(_f(p.get("unrealized_pl")) for p in self.legs)

    @property
    def profit_fraction(self) -> float | None:
        """Package P&L as a fraction of the max gain recorded at entry — the
        number the 60% profit target was always supposed to be measured
        against."""
        if self.max_gain <= 0:
            return None
        return self.unrealized_pl / self.max_gain

    def dte(self, today: date) -> int | None:
        return (self.expiration - today).days if self.expiration else None

    def closing_legs(self) -> list[dict]:
        """`--legs` payload that flattens this position in one mleg order.

        Built from symbols rather than Contract objects: closing must work from
        the journal record alone, without a live chain fetch, because the case
        that most needs closing is the one where market data is degraded.
        """
        out = []
        for pos in self.legs:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            is_long = _f(pos.get("qty")) > 0
            out.append({
                "symbol": symbol,
                "side": "sell" if is_long else "buy",
                "ratio_qty": 1,
                "position_intent": "sell_to_close" if is_long else "buy_to_close",
            })
        return out

    def describe(self) -> str:
        legs = " / ".join(self.symbols) or "(none)"
        return f"{self.underlying} {self.kind} x{self.qty} [{self.state}] {legs}"

    def as_log(self) -> dict:
        return {
            "underlying": self.underlying,
            "kind": self.kind,
            "expiration": str(self.expiration) if self.expiration else None,
            "long_leg": self.long_symbol,
            "short_leg": self.short_symbol,
            "qty": self.qty,
            "approved_qty": self.approved_qty,
            "excess_qty": self.excess_qty,
            "state": self.state,
            "max_loss": round(self.max_loss, 2),
            "max_gain": round(self.max_gain, 2),
            "unrealized_pl": round(self.unrealized_pl, 2),
            "profit_fraction": (round(self.profit_fraction, 4)
                                if self.profit_fraction is not None else None),
        }


def _leg_key(spec: dict) -> tuple[str, str]:
    return (str(spec.get("long_leg") or ""), str(spec.get("short_leg") or ""))


def _entries(decisions: list[dict]) -> list[dict]:
    """Journal records that opened a spread, newest first."""
    out = [d for d in decisions
           if d.get("action") == "opened" and isinstance(d.get("spread"), dict)]
    out.sort(key=lambda d: str(d.get("timestamp", "")), reverse=True)
    return out


def _authorised(decisions: list[dict]) -> dict[tuple[str, str], dict]:
    """Everything the gates ever approved for each pair of legs, summed.

    The first version of this took only the newest record and treated the rest
    as excess, which is wrong the moment the agent enters the same strikes
    twice. It did, on consecutive days: AAPL 260925 P305/P310 was opened for 4
    contracts on 2026-09-01 and 4 more on 2026-09-02, both separately approved.
    The report called the second four an over-fill and told an operator to trim
    a position that no gate had any objection to — a false alarm that
    recommends a destructive action, which is worse than no alarm at all.

    So the ceiling is the sum of every open, not the last one. A position is
    only flagged when it exceeds everything that was ever authorised for those
    legs, which is exactly the question `excess_qty` is meant to answer.

    The known limitation, stated rather than hidden: closes are not subtracted.
    Re-entering legs that were previously closed raises the ceiling and could
    mask a later over-fill. That direction is deliberate — a missed warning
    costs a warning, while a false one costs contracts.
    """
    out: dict[tuple[str, str], dict] = {}
    for record in _entries(decisions):
        spec = record["spread"]
        key = _leg_key(spec)
        bucket = out.setdefault(key, {"qty": 0, "max_loss": 0.0, "max_gain": 0.0,
                                      "entries": [], "newest": record})
        bucket["qty"] += max(int(_f(spec.get("qty"), 1)), 0)
        bucket["max_loss"] += _f(spec.get("max_loss"))
        bucket["max_gain"] += _f(spec.get("max_gain"))
        bucket["entries"].append(record)
    return out


def _worst_case_for_orphan(pos: dict) -> float:
    """Worst case for a leg with no structure behind it.

    A long option can lose what it cost. A short option is undefined-risk and
    is deliberately charged the full strike notional: the agent never opens
    one, so if the portfolio gate sees this number it should be large enough to
    stop everything until a human has looked.
    """
    qty = abs(_f(pos.get("qty")))
    if _f(pos.get("qty")) > 0:
        return abs(_f(pos.get("cost_basis")))
    try:
        _, _, _, strike = ch.parse_occ(pos.get("symbol", ""))
    except ValueError:
        return abs(_f(pos.get("cost_basis")))
    return strike * 100 * qty


def reconcile(positions: list[dict], decisions: list[dict] | None = None
              ) -> tuple[list[OpenSpread], list[OpenSpread]]:
    """Group broker legs into the spreads the journal says they belong to.

    Returns (spreads, unexplained) — the second list holds partials and orphans
    so a caller can report them rather than have them quietly vanish from the
    risk total.
    """
    records = (decisions if decisions is not None
               else journal.read(config.DECISIONS_LOG))

    option_legs: dict[str, dict] = {}
    for pos in positions:
        symbol = pos.get("symbol", "")
        try:
            ch.parse_occ(symbol)
        except ValueError:
            continue                       # equity position; not ours
        if abs(_f(pos.get("qty"))) == 0:
            continue
        option_legs[symbol] = pos

    spreads: list[OpenSpread] = []
    unexplained: list[OpenSpread] = []
    claimed: set[str] = set()

    for (long_symbol, short_symbol), bucket in _authorised(records).items():
        if long_symbol in claimed or short_symbol in claimed:
            continue

        legs = [option_legs[s] for s in (long_symbol, short_symbol)
                if s in option_legs]
        if not legs:
            continue                       # closed, or never filled

        claimed.update(p["symbol"] for p in legs)
        record = bucket["newest"]
        spec = record["spread"]
        expiration = None
        try:
            _, expiration, _, _ = ch.parse_occ(long_symbol or short_symbol or "")
        except ValueError:
            pass

        # Size from the broker, not the journal: a partial fill on the package
        # is possible even though mleg is meant to prevent it, and the broker
        # is authoritative about how many contracts are actually on.
        filled = min(abs(int(_f(p.get("qty")))) for p in legs)
        approved = max(int(bucket["qty"]), 0)
        # Risk is the authorised risk scaled to what is actually on. When the
        # two agree — the normal case, including several entries at the same
        # strikes — this is just the sum of what each entry recorded.
        scale = (filled / approved) if approved else 1.0

        spread = OpenSpread(
            underlying=spec.get("underlying") or "",
            kind=spec.get("kind") or "",
            expiration=expiration,
            long_symbol=long_symbol,
            short_symbol=short_symbol,
            qty=filled,
            max_loss=bucket["max_loss"] * scale,
            max_gain=bucket["max_gain"] * scale,
            legs=legs,
            state=MATCHED if len(legs) == 2 else PARTIAL,
            entry=record,
            approved_qty=approved,
        )
        spreads.append(spread)
        if spread.state == PARTIAL:
            unexplained.append(spread)

    for symbol, pos in option_legs.items():
        if symbol in claimed:
            continue
        try:
            underlying, expiration, _, _ = ch.parse_occ(symbol)
        except ValueError:
            continue
        orphan = OpenSpread(
            underlying=underlying, kind=ORPHAN, expiration=expiration,
            long_symbol=symbol if _f(pos.get("qty")) > 0 else None,
            short_symbol=symbol if _f(pos.get("qty")) < 0 else None,
            qty=abs(int(_f(pos.get("qty")))),
            max_loss=_worst_case_for_orphan(pos), max_gain=0.0,
            legs=[pos], state=ORPHAN,
        )
        spreads.append(orphan)
        unexplained.append(orphan)

    return spreads, unexplained


def open_risk(spreads: list[OpenSpread]) -> float:
    """Total dollars at risk across everything currently on.

    This replaces `sum(abs(cost_basis))`, which answered a different question
    and got credit spreads badly wrong in the safe-looking direction.
    """
    return sum(s.max_loss for s in spreads)


def risk_by_underlying(spreads: list[OpenSpread]) -> dict[str, float]:
    """Dollars at risk per underlying.

    The number the per-trade cap cannot see. Each entry is gated on its own and
    passes at 2% of equity; nothing was watching what they added up to in one
    name until AAPL reached 5.2% across two separately-approved entries at the
    same strikes — which is one position in every sense except the journal's.
    """
    out: dict[str, float] = {}
    for spread in spreads:
        key = (spread.underlying or "").upper()
        if not key:
            continue
        out[key] = out.get(key, 0.0) + spread.max_loss
    return out
