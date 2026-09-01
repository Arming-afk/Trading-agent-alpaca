"""Getting a submitted spread to actually trade.

A decision that does not become a position is not a conservative decision. It
is a missing row in the record, and on 2026-08-31 it was two rows out of three:
the agent priced QQQ at $2.81 and AAPL at $0.39, submitted both as day orders
seventeen minutes before the close, and neither ever traded. The decision log
called that day three trades. The account had one.

Nothing about those two decisions was wrong. The pricing was wrong, and only in
the sense that a single limit sent once is a bid, not an execution strategy —
a two-leg option package quoted a dime wide on each leg will not fill at the
midpoint just because the midpoint is fair.

So the agent walks its own price. It submits at a modest concession, waits, and
if the package has not traded it cancels and re-submits closer to the
marketable side, up to a stated maximum. The concession is bounded by the same
budget the trade was sized against, which is the part that is easy to get
wrong: **a worse price is more risk.** Re-pricing a debit spread upward without
re-sizing spends risk budget that no gate ever approved, so every re-quote is
re-sized at the new price and the position shrinks if it has to. A chase that
cannot fit inside the budget is abandoned, not forced.

The whole loop is skipped when trading is disabled, and every step is written
to the decision log — including the abandonment, which is the outcome the
previous version recorded as a trade.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from agent import cli, spreads

logger = logging.getLogger(__name__)

#: How many times to improve the price before giving up.
DEFAULT_ROUNDS = 3
#: Seconds to leave an order resting before deciding it will not fill.
DEFAULT_WAIT = 60
#: How much closer to the marketable price each round moves, as a fraction of
#: the mid-to-natural distance. Starting at 0.5, three rounds reach 1.0 —
#: crossing both markets, which is the worst price the package can trade at and
#: the point past which there is nothing left to concede.
DEFAULT_STEP = 0.25

FILLED = "filled"
PARTIAL = "partially_filled"
WORKING = "working"
ABANDONED = "abandoned"

#: Order states that mean the order is no longer live.
_DEAD = {"canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced"}


@dataclass
class Pending:
    """One submitted spread the runner is still waiting on."""
    symbol: str
    spread: spreads.Vertical
    order_id: str
    limit_price: float
    aggression: float
    risk_budget: float
    #: One entry per re-quote: what changed and why.
    attempts: list[dict] = field(default_factory=list)
    status: str = WORKING
    filled_qty: int = 0

    @property
    def risk_at_limit(self) -> float:
        return self.spread.max_loss_at(self.limit_price)

    def as_log(self) -> dict:
        return {
            "order_id": self.order_id,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "final_limit": round(self.limit_price, 2),
            "final_aggression": round(self.aggression, 2),
            "risk_at_final_limit": round(self.risk_at_limit, 2),
            "attempts": self.attempts,
        }


def _order_state(order: dict) -> tuple[str, int]:
    status = str(order.get("status") or "").lower()
    filled = int(float(order.get("filled_qty") or 0))
    return status, filled


def poll(order_id: str) -> tuple[str, int]:
    """Current status and filled quantity, or ("unknown", 0) if unreadable."""
    try:
        return _order_state(cli.get_order(order_id))
    except cli.AlpacaCLIError as exc:
        logger.warning("could not read order %s: %s", order_id, exc)
        return "unknown", 0


def _cancel(order_id: str) -> None:
    try:
        cli.cancel_order(order_id)
    except cli.AlpacaCLIError as exc:
        # A cancel that fails because the order already went away is the good
        # case, not an error worth stopping for.
        logger.info("cancel of %s returned: %s", order_id, exc)


def chase(pending: list[Pending], *, rounds: int = DEFAULT_ROUNDS,
          wait_seconds: int = DEFAULT_WAIT, step: float = DEFAULT_STEP,
          sleep=time.sleep) -> list[Pending]:
    """Walk unfilled orders toward the market until they trade or run out of room.

    Returns the same objects, with `status` and `attempts` filled in. `sleep`
    is injectable so the tests can run the whole loop without waiting.
    """
    live = [p for p in pending if p.status == WORKING]

    for round_no in range(1, rounds + 1):
        if not live:
            break
        sleep(wait_seconds)

        still_working: list[Pending] = []
        for item in live:
            status, filled = poll(item.order_id)
            item.filled_qty = filled

            if status == "filled" or filled >= item.spread.qty:
                item.status = FILLED
                item.attempts.append({"round": round_no, "action": "filled",
                                      "limit": round(item.limit_price, 2)})
                logger.info("%s filled at %.2f", item.symbol, item.limit_price)
                continue

            if filled > 0:
                # A partial on an mleg package is rare and is not something to
                # improve on automatically: the position is already on and its
                # shape is not the one that was sized. Leave it and report.
                item.status = PARTIAL
                item.attempts.append({"round": round_no, "action": "partial",
                                      "filled_qty": filled})
                logger.warning("%s partially filled: %d of %d",
                               item.symbol, filled, item.spread.qty)
                continue

            if status in _DEAD:
                item.status = ABANDONED
                item.attempts.append({"round": round_no, "action": "order_died",
                                      "broker_status": status})
                continue

            if item.aggression >= 1.0:
                # Already crossing both markets. There is no better price to
                # offer, and paying through the offer is how a defined-risk
                # trade quietly stops being worth doing.
                _cancel(item.order_id)
                item.status = ABANDONED
                item.attempts.append({"round": round_no, "action": "abandoned",
                                      "why": "already at the marketable price"})
                logger.info("%s abandoned — no price left to concede", item.symbol)
                continue

            requote = _requote(item, step)
            item.attempts.append(requote)
            if requote["action"] == "abandoned":
                item.status = ABANDONED
                continue
            still_working.append(item)

        live = still_working

    # Anything still working when the rounds run out keeps its resting order:
    # a day order that fills after the run ends is a fill, and cancelling it
    # would throw away the position to tidy up the log.
    for item in live:
        item.attempts.append({"action": "left_resting",
                              "limit": round(item.limit_price, 2)})
    return pending


def _requote(item: Pending, step: float) -> dict:
    """Cancel and re-submit one order at a better price, re-sized to fit.

    The re-size is the point. A debit spread re-quoted from $1.31 to $1.38 is
    5% more risk per contract, and at the original quantity that is budget the
    risk gate never granted. So the quantity is recomputed against the same
    budget at the new price, and if one contract no longer fits, the trade is
    abandoned rather than shrunk to something the gate would not recognise.
    """
    aggression = min(item.aggression + step, 1.0)
    new_limit = item.spread.limit_price(aggression)
    qty = spreads.size_for_risk(item.spread, item.risk_budget, net_price=new_limit)

    if qty < 1:
        _cancel(item.order_id)
        return {"action": "abandoned", "why": "no size fits the budget at the "
                f"improved price {new_limit:.2f}", "limit": round(new_limit, 2)}

    _cancel(item.order_id)
    resized = spreads.Vertical(item.spread.kind, item.spread.long_leg,
                               item.spread.short_leg, qty=qty)
    try:
        order = cli.submit_mleg(resized.legs_payload(), qty=qty,
                                limit_price=new_limit)
    except cli.AlpacaCLIError as exc:
        return {"action": "abandoned", "why": f"re-submit rejected: {exc}",
                "limit": round(new_limit, 2)}

    previous_qty = item.spread.qty
    item.spread = resized
    item.order_id = str(order.get("id") or item.order_id)
    item.limit_price = new_limit
    item.aggression = aggression

    return {
        "action": "requoted",
        "limit": round(new_limit, 2),
        "aggression": round(aggression, 2),
        "qty": qty,
        "qty_was": previous_qty,
        "risk_at_limit": round(resized.max_loss_at(new_limit), 2),
        "order_id": item.order_id,
    }


def summarise(pending: list[Pending]) -> dict:
    """What the chase achieved, for the run record."""
    return {
        "submitted": len(pending),
        "filled": sum(1 for p in pending if p.status == FILLED),
        "partial": sum(1 for p in pending if p.status == PARTIAL),
        "abandoned": sum(1 for p in pending if p.status == ABANDONED),
        "still_working": sum(1 for p in pending if p.status == WORKING),
    }


# ── closing ──────────────────────────────────────────────────────────────────
# Closing has the opposite failure mode to opening. An open that does not fill
# costs an opportunity; a close that does not fill leaves risk on that the agent
# has already decided it does not want. So the concession here is larger, and
# the fallback is louder.

#: Fraction of the package mark conceded to get out, on top of the mark itself.
CLOSE_CONCESSION_PCT = 4.0
#: Never concede less than this, however cheap the package — a two-cent
#: concession on a package quoted a dime wide is not an exit.
MIN_CLOSE_CONCESSION = 0.05


def close_price(spread, *, concession_pct: float = CLOSE_CONCESSION_PCT) -> float | None:
    """Limit price for flattening `spread`, from the broker's own marks.

    Deliberately computed from the position payload rather than from a fresh
    chain fetch: the run that most needs to close a position is the one where
    market data is already degraded, and an exit that depends on the same
    fetch that just failed is not an exit.
    """
    net = 0.0
    seen = 0
    for pos in spread.legs:
        price = pos.get("current_price")
        if price in (None, ""):
            return None
        try:
            per_share = float(price)
        except (TypeError, ValueError):
            return None
        qty = float(pos.get("qty") or 0)
        # Closing a long leg pays us; closing a short leg costs us.
        net += per_share if qty < 0 else -per_share
        seen += 1
    if seen == 0:
        return None

    concession = max(abs(net) * concession_pct / 100, MIN_CLOSE_CONCESSION)
    target = abs(net) + concession if net > 0 else abs(net) - concession
    return max(round(target, 2), 0.01)


def close_spread(spread, *, reason: str = "") -> dict:
    """Flatten one reconstructed spread with a single mleg order.

    One order, not two. Closing the legs separately is how a defined-risk
    position becomes an undefined one for however long the second order takes
    to fill — and on the 5-DTE exit, "however long" is the window the exit
    exists to avoid.
    """
    legs = spread.closing_legs()
    if len(legs) < 2:
        # A partial or an orphan cannot be closed as a package. Say so and let
        # the caller decide; guessing here would submit a one-legged order that
        # looks like a spread in the log.
        return {"submitted": False,
                "why": f"{len(legs)} leg(s) — not a closable package",
                "reason": reason}

    limit = close_price(spread)
    if limit is None:
        return {"submitted": False,
                "why": "no mark on one or more legs — cannot price the exit",
                "reason": reason}

    try:
        order = cli.submit_mleg(legs, qty=spread.qty, limit_price=limit)
    except cli.AlpacaCLIError as exc:
        return {"submitted": False, "why": f"close rejected: {exc}", "reason": reason}

    return {"submitted": True, "order": order, "limit": limit,
            "qty": spread.qty, "reason": reason,
            "command": cli.as_argv("order", "submit", "--order-class", "mleg",
                                   "--type", "limit", "--qty", str(spread.qty),
                                   "--limit-price", f"{limit:.2f}",
                                   "--time-in-force", "day", "--legs", "<legs.json>")}
