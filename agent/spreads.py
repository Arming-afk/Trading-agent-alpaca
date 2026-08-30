"""Vertical spread construction and its defined-risk arithmetic.

Every position this agent opens is a two-leg vertical. That is a deliberate
constraint, not a limitation of the code: a vertical's worst case is known at
submission time, in dollars, before any order is sent. The risk gates in
agent/risk.py are only meaningful because `max_loss` below is exact rather
than a model estimate — an undefined-risk structure (a naked short put, say)
would make every portfolio-level guard in this project a guess.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.chain import Contract

#: One US equity option contract covers 100 shares.
MULTIPLIER = 100

# Spread kinds, named by direction and cash flow.
BULL_CALL_DEBIT = "bull_call_debit"     # long lower call  / short higher call
BEAR_PUT_DEBIT = "bear_put_debit"       # long higher put  / short lower put
BULL_PUT_CREDIT = "bull_put_credit"     # short higher put / long lower put
BEAR_CALL_CREDIT = "bear_call_credit"   # short lower call / long higher call

DEBIT_KINDS = (BULL_CALL_DEBIT, BEAR_PUT_DEBIT)
CREDIT_KINDS = (BULL_PUT_CREDIT, BEAR_CALL_CREDIT)


@dataclass(frozen=True)
class Vertical:
    """A two-leg vertical spread, priced at the current market."""
    kind: str
    long_leg: Contract
    short_leg: Contract
    qty: int = 1

    def __post_init__(self):
        if self.long_leg.kind != self.short_leg.kind:
            raise ValueError("a vertical's legs must be the same option type")
        if self.long_leg.expiration != self.short_leg.expiration:
            raise ValueError("a vertical's legs must share an expiration")
        if self.long_leg.strike == self.short_leg.strike:
            raise ValueError("a vertical needs two different strikes")
        if self.qty < 1:
            raise ValueError("qty must be at least 1")

    @property
    def is_debit(self) -> bool:
        return self.kind in DEBIT_KINDS

    @property
    def width(self) -> float:
        """Strike distance, in dollars per share."""
        return abs(self.long_leg.strike - self.short_leg.strike)

    @property
    def net_mid(self) -> float:
        """Net premium per share at the midpoint: positive = we pay a debit,
        negative = we collect a credit."""
        return self.long_leg.mid - self.short_leg.mid

    @property
    def max_loss(self) -> float:
        """Worst case for the whole position, in dollars. Exact, not modelled."""
        if self.is_debit:
            per_share = max(self.net_mid, 0.0)
        else:
            per_share = max(self.width + self.net_mid, 0.0)
        return per_share * MULTIPLIER * self.qty

    @property
    def max_gain(self) -> float:
        if self.is_debit:
            per_share = max(self.width - self.net_mid, 0.0)
        else:
            per_share = max(-self.net_mid, 0.0)
        return per_share * MULTIPLIER * self.qty

    @property
    def breakeven(self) -> float:
        """Underlying price at expiry where the position breaks even."""
        if self.kind == BULL_CALL_DEBIT:
            return self.long_leg.strike + self.net_mid
        if self.kind == BEAR_PUT_DEBIT:
            return self.long_leg.strike - self.net_mid
        if self.kind == BULL_PUT_CREDIT:
            return self.short_leg.strike + self.net_mid
        return self.short_leg.strike - self.net_mid    # BEAR_CALL_CREDIT

    @property
    def reward_risk(self) -> float:
        loss = self.max_loss
        return self.max_gain / loss if loss > 0 else 0.0

    @property
    def worst_spread_pct(self) -> float:
        """The wider of the two legs' bid/ask spreads — the liquidity of a
        spread is the liquidity of its worse leg."""
        return max(self.long_leg.spread_pct, self.short_leg.spread_pct)

    def legs_payload(self, *, closing: bool = False) -> list[dict]:
        """Legs in the shape `alpaca order submit --legs` expects."""
        if closing:
            long_intent, short_intent = "sell_to_close", "buy_to_close"
            long_side, short_side = "sell", "buy"
        else:
            long_intent, short_intent = "buy_to_open", "sell_to_open"
            long_side, short_side = "buy", "sell"
        return [
            {"symbol": self.long_leg.symbol, "side": long_side,
             "ratio_qty": 1, "position_intent": long_intent},
            {"symbol": self.short_leg.symbol, "side": short_side,
             "ratio_qty": 1, "position_intent": short_intent},
        ]

    def limit_price(self, slippage_pct: float = 5.0) -> float:
        """Limit price for the spread, walked slightly away from mid so the
        order can actually fill without paying the full width of both markets.

        For a debit we are willing to pay a little above mid; for a credit we
        accept a little below. `--type limit` on an mleg order prices the net
        of the package, always as a positive number.
        """
        net = abs(self.net_mid)
        adjusted = net * (1 + slippage_pct / 100) if self.is_debit else net * (1 - slippage_pct / 100)
        return max(round(adjusted, 2), 0.01)

    def describe(self) -> str:
        a, b = self.long_leg, self.short_leg
        verb = "debit" if self.is_debit else "credit"
        return (
            f"{self.kind} {self.qty}x {a.underlying} "
            f"{a.expiration:%Y-%m-%d} {a.strike:g}/{b.strike:g} {a.kind} "
            f"@ {abs(self.net_mid):.2f} {verb} "
            f"(max loss ${self.max_loss:.0f}, max gain ${self.max_gain:.0f})"
        )


def build(kind: str, legs: list[Contract], qty: int = 1) -> Vertical:
    """Assemble a Vertical of `kind` from two same-type, same-expiry contracts,
    assigning long/short by the definition of that spread."""
    if len(legs) != 2:
        raise ValueError(f"a vertical takes exactly 2 contracts, got {len(legs)}")
    lower, upper = sorted(legs, key=lambda c: c.strike)

    if kind == BULL_CALL_DEBIT:
        long_leg, short_leg = lower, upper
    elif kind == BEAR_PUT_DEBIT:
        long_leg, short_leg = upper, lower
    elif kind == BULL_PUT_CREDIT:
        long_leg, short_leg = lower, upper
    elif kind == BEAR_CALL_CREDIT:
        long_leg, short_leg = upper, lower
    else:
        raise ValueError(f"unknown spread kind: {kind!r}")

    return Vertical(kind=kind, long_leg=long_leg, short_leg=short_leg, qty=qty)


def size_for_risk(spread: Vertical, risk_budget: float) -> int:
    """Largest whole-contract quantity whose max loss fits `risk_budget`.

    Returns 0 when even one contract is too large — the caller must treat that
    as "no trade", never as "round up to one".
    """
    unit = Vertical(kind=spread.kind, long_leg=spread.long_leg,
                    short_leg=spread.short_leg, qty=1)
    if unit.max_loss <= 0 or risk_budget <= 0:
        return 0
    return int(risk_budget // unit.max_loss)
