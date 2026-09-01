"""Per-trade outcomes, joined back to the reading that caused the trade.

The README has always said the strategy is falsifiable: *if the credit spreads
lose money while IV/RV was above the threshold at entry, the premium was not
actually rich.* That sentence was a promise the code could not keep. Every
entry logged its IV, RV and ratio, and nothing ever computed what happened
next, so the record could describe eight decisions and not one result.

This module closes that loop. It takes the decision log as the record of *what
was believed*, the broker as the record of *what is on*, and joins them into
one row per position: the regime reading at entry, and the dollars that
followed from it.

Three states, kept distinct on purpose:

* **open** — both legs still at the broker; P&L is the mark, not a result.
* **closed** — the journal has a closing record; P&L is realized.
* **unfilled** — the order was submitted and never traded. These are not
  failures of the thesis and must not be averaged into it, but they are also
  not nothing: two of the first three live orders never filled, which is a fact
  about the execution path and belongs in the record as one.

The aggregate is deliberately blunt. Five trading days cannot establish an
edge, and this module does not pretend otherwise — `verdict()` says so in as
many words when the sample is too small, rather than reporting a win rate that
would be read as evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agent import chain as ch
from agent import config, journal

OPEN = "open"
CLOSED = "closed"
UNFILLED = "unfilled"
UNRESOLVED = "unresolved"

#: Below this many resolved trades, no aggregate is reported as evidence.
MIN_SAMPLE_FOR_A_CLAIM = 20


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Outcome:
    """One position: what was believed at entry, and what came of it."""
    symbol: str
    opened_at: str
    kind: str
    stance: str
    implied_vol: float | None
    realized_vol: float | None
    ratio: float | None
    qty: int
    max_loss: float
    max_gain: float
    status: str
    pl: float = 0.0
    long_symbol: str | None = None
    short_symbol: str | None = None
    expiration: str | None = None
    note: str = ""

    @property
    def is_credit(self) -> bool:
        return self.kind.endswith("_credit")

    @property
    def resolved(self) -> bool:
        """Carries information about the thesis. An unfilled order does not."""
        return self.status in (OPEN, CLOSED)

    @property
    def pl_vs_risk(self) -> float | None:
        """P&L as a fraction of the dollars that were actually at risk — the
        only way trades of different sizes compare.

        None, not zero, when the position never resolved. An order that never
        filled did not break even; it did not happen, and rendering it as 0.0%
        puts a result in the table where there is no result.
        """
        if not self.resolved or self.max_loss <= 0:
            return None
        return self.pl / self.max_loss

    def as_log(self) -> dict:
        return {
            "symbol": self.symbol,
            "opened_at": self.opened_at,
            "kind": self.kind,
            "stance": self.stance,
            "implied_vol": self.implied_vol,
            "realized_vol": self.realized_vol,
            "iv_rv_ratio": self.ratio,
            "qty": self.qty,
            "max_loss": round(self.max_loss, 2),
            "max_gain": round(self.max_gain, 2),
            "status": self.status,
            "pl": round(self.pl, 2),
            "pl_vs_risk": (round(self.pl_vs_risk, 4)
                           if self.pl_vs_risk is not None else None),
            "long_leg": self.long_symbol,
            "short_leg": self.short_symbol,
            "expiration": self.expiration,
            "note": self.note,
        }


def _closing_records(decisions: list[dict]) -> dict[str, list[dict]]:
    """Closing records keyed by the leg symbol they closed."""
    out: dict[str, list[dict]] = {}
    for row in decisions:
        if row.get("action") != "closed":
            continue
        symbol = row.get("symbol") or ""
        out.setdefault(symbol, []).append(row)
    return out


def build(decisions: list[dict] | None = None,
          positions: list[dict] | None = None,
          orders: dict[str, dict] | None = None) -> list[Outcome]:
    """One Outcome per submitted spread, oldest first.

    `orders` maps an order id to the broker's current view of that order, and
    is what separates "never filled" from "closed without a record". Without
    it those two collapse into `unresolved`, which is honest but less useful —
    so `scripts/outcomes.py` always passes it.
    """
    records = decisions if decisions is not None else journal.read(config.DECISIONS_LOG)
    live = {p.get("symbol"): p for p in (positions or [])}
    closes = _closing_records(records)
    order_view = orders or {}

    out: list[Outcome] = []
    for row in records:
        if row.get("action") != "opened":
            continue
        spec = row.get("spread") or {}
        regime = row.get("regime") or {}
        order = row.get("order") or {}

        long_symbol = spec.get("long_leg")
        short_symbol = spec.get("short_leg")
        legs = [live[s] for s in (long_symbol, short_symbol) if s in live]

        qty = int(_f(spec.get("qty"), 1))
        max_loss = _f(spec.get("max_loss"))
        max_gain = _f(spec.get("max_gain"))

        if legs:
            status = OPEN
            pl = sum(_f(p.get("unrealized_pl")) for p in legs)
            note = "marked to market" if len(legs) == 2 else "one leg open"
        else:
            broker_order = order_view.get(str(order.get("id") or ""), {})
            filled = _f(broker_order.get("filled_qty"), -1.0)
            closed_for = [r for s in (long_symbol, short_symbol)
                          for r in closes.get(s, [])]
            if filled == 0:
                status, pl = UNFILLED, 0.0
                note = f"order {broker_order.get('status', 'expired')} without a fill"
            elif closed_for:
                status, pl = CLOSED, 0.0
                note = "; ".join(r.get("reason", "") for r in closed_for)[:200]
            else:
                status, pl = UNRESOLVED, 0.0
                note = "no broker position and no closing record"

        out.append(Outcome(
            symbol=row.get("symbol") or spec.get("underlying") or "",
            opened_at=str(row.get("timestamp") or ""),
            kind=spec.get("kind") or "",
            stance=regime.get("stance") or "",
            implied_vol=regime.get("implied_vol"),
            realized_vol=regime.get("realized_vol"),
            ratio=regime.get("iv_rv_ratio"),
            qty=qty, max_loss=max_loss, max_gain=max_gain,
            status=status, pl=pl,
            long_symbol=long_symbol, short_symbol=short_symbol,
            expiration=spec.get("expiration"), note=note,
        ))

    out.sort(key=lambda o: o.opened_at)
    return out


@dataclass
class Bucket:
    """Aggregate for one group of trades."""
    label: str
    trades: list[Outcome] = field(default_factory=list)

    @property
    def resolved(self) -> list[Outcome]:
        return [t for t in self.trades if t.resolved]

    @property
    def pl(self) -> float:
        return sum(t.pl for t in self.resolved)

    @property
    def risk(self) -> float:
        return sum(t.max_loss for t in self.resolved)

    @property
    def return_on_risk(self) -> float | None:
        return self.pl / self.risk if self.risk > 0 else None

    @property
    def wins(self) -> int:
        return sum(1 for t in self.resolved if t.pl > 0)

    def as_log(self) -> dict:
        return {
            "label": self.label,
            "trades": len(self.trades),
            "resolved": len(self.resolved),
            "unfilled": sum(1 for t in self.trades if t.status == UNFILLED),
            "wins": self.wins,
            "pl": round(self.pl, 2),
            "risk_deployed": round(self.risk, 2),
            "return_on_risk": (round(self.return_on_risk, 4)
                               if self.return_on_risk is not None else None),
        }


def by_stance(outcomes: list[Outcome]) -> dict[str, Bucket]:
    """Split by what the agent believed: selling rich premium, or buying cheap.

    This is the split that matters, because the two halves rest on different
    claims. The variance risk premium is a documented effect and the credit
    side tests it. The debit side is a directional bet with a volatility
    trigger, and pooling the two would let one hide inside the other.
    """
    buckets: dict[str, Bucket] = {}
    for o in outcomes:
        label = o.stance or "unknown"
        buckets.setdefault(label, Bucket(label)).trades.append(o)
    return buckets


def verdict(outcomes: list[Outcome]) -> str:
    """The falsification question, answered from the record or declined.

    Declining is the common and correct answer over a five-day window. A win
    rate computed on three trades is not weak evidence; it is not evidence, and
    reporting it as though it were is the failure mode this whole project is
    written against.
    """
    buckets = by_stance(outcomes)
    credit = buckets.get("sell_premium", Bucket("sell_premium"))
    resolved = credit.resolved

    if not resolved:
        return ("No credit spread has resolved yet — the thesis is untested. "
                "The record shows decisions, not results.")

    if len(resolved) < MIN_SAMPLE_FOR_A_CLAIM:
        direction = "profitable" if credit.pl > 0 else "unprofitable"
        return (f"{len(resolved)} credit spread(s), {direction} by "
                f"${credit.pl:,.0f} on ${credit.risk:,.0f} of risk. That is far "
                f"below the {MIN_SAMPLE_FOR_A_CLAIM} needed to distinguish the "
                f"thesis from noise, and is reported as a fact about this week, "
                f"not as evidence about the premium.")

    ror = credit.return_on_risk or 0.0
    if ror < 0:
        return (f"Credit spreads returned {ror:.1%} on risk across "
                f"{len(resolved)} trades while IV/RV was above the threshold at "
                f"entry. On the stated falsification test, that is evidence "
                f"against the thresholds — not against the direction calls.")
    return (f"Credit spreads returned {ror:.1%} on risk across "
            f"{len(resolved)} trades. Consistent with the thesis; not yet "
            f"sufficient to establish it.")


def summarise(outcomes: list[Outcome]) -> dict:
    """The whole report as one record, for the log and the dashboard."""
    resolved = [o for o in outcomes if o.resolved]
    return {
        "generated": journal.now_iso(),
        "trades_submitted": len(outcomes),
        "filled": len(resolved),
        "unfilled": sum(1 for o in outcomes if o.status == UNFILLED),
        "fill_rate": (round(len(resolved) / len(outcomes), 4) if outcomes else None),
        "total_pl": round(sum(o.pl for o in resolved), 2),
        "by_stance": {k: b.as_log() for k, b in by_stance(outcomes).items()},
        "verdict": verdict(outcomes),
    }


def write(outcomes: list[Outcome], summary: dict | None = None) -> None:
    """Rewrite logs/outcomes.jsonl. Unlike the decision log this file is a
    derived view, not an append-only record — it is regenerated from the
    journal every time so it can never disagree with it."""
    path = config.LOGS / "outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [o.as_log() for o in outcomes]
    if summary is not None:
        lines.append({"record": "summary", **summary})
    import json
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in lines) + "\n",
        encoding="utf-8")


def expired_today(outcomes: list[Outcome], today: date) -> list[Outcome]:
    """Positions whose expiry has passed — a reconciliation prompt, since an
    expired spread leaves no broker position and no closing record."""
    out = []
    for o in outcomes:
        if not o.expiration:
            continue
        try:
            exp = date.fromisoformat(o.expiration)
        except ValueError:
            continue
        if exp < today and o.status == UNRESOLVED:
            out.append(o)
    return out
