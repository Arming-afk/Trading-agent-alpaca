"""The trading logic: which spread to open on which underlying, and why.

## The thesis

The agent does not try to predict direction. The previous incarnation of this
codebase spent eight pre-registered experiments looking for a directional edge
in large-cap equities and found none that survived honest inference, so betting
this entry on one would be repeating a finished experiment.

Instead it trades a **structural** claim: options are usually priced above the
volatility that subsequently arrives. When the market is paying well above
recent realized movement, the agent sells a defined-risk credit spread and lets
time decay work. When options are unusually cheap relative to realized movement,
it buys a debit spread instead. When neither holds, it does nothing — and doing
nothing is the most common outcome by design.

## The regime map

    IV/RV >= RICH_RATIO   →  sell premium   (credit spread, short leg OTM)
    IV/RV <= CHEAP_RATIO  →  buy premium    (debit spread, long leg near ATM)
    otherwise             →  no trade

Direction is chosen by a blunt trend filter (`vol.trend_bias`) whose only job is
to keep the agent from selling puts into a sustained decline. It is not a
forecast and carries no claim of edge.

## What would falsify this

If the credit spreads lose money while IV/RV was above the threshold at entry,
the premium was not actually rich — the thresholds, not the direction calls,
were wrong. Every entry logs its IV, RV, ratio and regime so that question can
be answered from the record rather than from memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agent import spreads, vol
from agent.chain import Contract, by_delta, by_strike, is_tradable
from agent.spreads import Vertical

# ── regime thresholds (a-priori; see README on why these are not tuned) ──────
#: Above this, implied vol is rich enough relative to realized to sell.
RICH_RATIO = 1.25
#: Below this, options are cheap enough relative to realized to buy.
CHEAP_RATIO = 0.95

#: Short leg of a credit spread sits around this delta — roughly a 4-in-5
#: chance of expiring worthless, before accounting for the premium collected.
SHORT_LEG_DELTA = 0.20
#: Long leg of a debit spread sits near the money.
LONG_LEG_DELTA = 0.50

#: Target days to expiry. Long enough to avoid expiry-week gamma, short enough
#: that decay is meaningful inside a one-week competition.
TARGET_DTE = 21

SELL_PREMIUM = "sell_premium"
BUY_PREMIUM = "buy_premium"
STAND_ASIDE = "stand_aside"


@dataclass
class Regime:
    """What the volatility surface says about one underlying, right now."""
    symbol: str
    implied_vol: float | None
    realized_vol: float | None
    ratio: float | None
    bias: str
    stance: str
    reason: str

    def as_log(self) -> dict:
        return {
            "symbol": self.symbol,
            "implied_vol": self.implied_vol,
            "realized_vol": self.realized_vol,
            "iv_rv_ratio": self.ratio,
            "trend_bias": self.bias,
            "stance": self.stance,
            "reason": self.reason,
        }


@dataclass
class Candidate:
    """A proposed trade, before risk sizing."""
    regime: Regime
    spread: Vertical
    rationale: str
    notes: list[str] = field(default_factory=list)


def classify(symbol: str, *, implied: float | None, realized: float | None,
             closes: list[float],
             rich_ratio: float = RICH_RATIO,
             cheap_ratio: float = CHEAP_RATIO) -> Regime:
    """Map the IV/RV reading onto a stance. Missing data means stand aside."""
    ratio = vol.variance_premium(implied, realized)
    bias = vol.trend_bias(closes)

    if ratio is None:
        stance, reason = STAND_ASIDE, "no IV/RV reading (missing chain greeks or bars)"
    elif ratio >= rich_ratio:
        stance = SELL_PREMIUM
        reason = f"IV/RV {ratio:.2f} >= {rich_ratio:.2f} — premium is rich"
    elif ratio <= cheap_ratio:
        stance = BUY_PREMIUM
        reason = f"IV/RV {ratio:.2f} <= {cheap_ratio:.2f} — premium is cheap"
    else:
        stance = STAND_ASIDE
        reason = f"IV/RV {ratio:.2f} inside [{cheap_ratio:.2f}, {rich_ratio:.2f}] — no edge claimed"

    return Regime(symbol=symbol, implied_vol=implied, realized_vol=realized,
                  ratio=ratio, bias=bias, stance=stance, reason=reason)


def spread_kind_for(stance: str, bias: str) -> str | None:
    """Regime + direction → which of the four verticals to build.

    A neutral bias sells puts, because the premium-selling case rests on decay
    rather than direction and a bull put credit spread is the structure whose
    worst case is a decline the trend filter would already have flagged.
    """
    if stance == SELL_PREMIUM:
        return spreads.BEAR_CALL_CREDIT if bias == "bearish" else spreads.BULL_PUT_CREDIT
    if stance == BUY_PREMIUM:
        if bias == "bullish":
            return spreads.BULL_CALL_DEBIT
        if bias == "bearish":
            return spreads.BEAR_PUT_DEBIT
        return None      # buying premium with no directional view is a coin flip
    return None


def _option_type_for(kind: str) -> str:
    return "put" if kind in (spreads.BULL_PUT_CREDIT, spreads.BEAR_PUT_DEBIT) else "call"


def select_legs(kind: str, contracts: list[Contract], spot: float,
                *, width_target: float | None = None,
                today: date | None = None) -> tuple[Contract, Contract] | None:
    """Pick the two strikes for `kind` from an already liquidity-filtered chain.

    The anchor leg is chosen by delta when the chain carries greeks and by
    moneyness otherwise, so a chain without greeks degrades to a wider but still
    valid selection instead of failing outright.
    """
    option_type = _option_type_for(kind)
    pool = [c for c in contracts if c.kind == option_type]
    if len(pool) < 2 or spot <= 0:
        return None

    is_credit = kind in spreads.CREDIT_KINDS
    target_delta = SHORT_LEG_DELTA if is_credit else LONG_LEG_DELTA

    anchor = by_delta(pool, target_delta)
    if anchor is None:
        # No greeks: approximate. A 0.20-delta strike sits roughly 5% OTM on a
        # three-week expiry for these names; 0.50-delta is at the money.
        offset = 0.05 if is_credit else 0.0
        if option_type == "put":
            anchor = by_strike(pool, spot * (1 - offset))
        else:
            anchor = by_strike(pool, spot * (1 + offset))
    if anchor is None:
        return None

    # The protective leg sits further out of the money, one strike increment
    # away by default so the defined risk stays small.
    strikes = sorted({c.strike for c in pool})
    increment = _strike_increment(strikes) if width_target is None else width_target
    if increment <= 0:
        return None

    if option_type == "put":
        # For puts, "further OTM" means a lower strike.
        protective_strike = anchor.strike - increment
    else:
        protective_strike = anchor.strike + increment

    protective = by_strike([c for c in pool if c.symbol != anchor.symbol],
                           protective_strike)
    if protective is None or protective.strike == anchor.strike:
        return None

    return anchor, protective


def _strike_increment(strikes: list[float]) -> float:
    """Most common gap between adjacent listed strikes."""
    if len(strikes) < 2:
        return 0.0
    gaps = [round(b - a, 2) for a, b in zip(strikes, strikes[1:]) if b > a]
    if not gaps:
        return 0.0
    return max(set(gaps), key=gaps.count)


def propose(regime: Regime, contracts: list[Contract], spot: float,
            *, today: date | None = None) -> Candidate | None:
    """Turn a regime reading into a concrete, priced spread — or nothing.

    Returns None whenever the regime says stand aside, the chain cannot supply
    two tradable strikes, or the resulting spread is priced nonsensically. The
    caller then records the reason and moves on; a missing candidate is a normal
    outcome, not an error.
    """
    kind = spread_kind_for(regime.stance, regime.bias)
    if kind is None:
        return None

    tradable = [c for c in contracts if is_tradable(c, today=today)]
    legs = select_legs(kind, tradable, spot, today=today)
    if legs is None:
        return None

    try:
        spread = spreads.build(kind, list(legs))
    except ValueError:
        return None

    notes = []
    # A credit spread that collects nothing carries the full width as risk for
    # no compensation; a debit spread priced at the full width has no upside.
    if spread.max_gain <= 0 or spread.max_loss <= 0:
        return None
    if spread.kind in spreads.CREDIT_KINDS and spread.reward_risk < 0.15:
        notes.append(f"thin credit: reward/risk {spread.reward_risk:.2f}")
    if spread.worst_spread_pct > 15:
        notes.append(f"wide market on a leg: {spread.worst_spread_pct:.1f}%")

    rationale = (
        f"{regime.reason}; trend {regime.bias} → {kind} "
        f"{spread.long_leg.strike:g}/{spread.short_leg.strike:g} "
        f"{spread.long_leg.expiration:%Y-%m-%d}"
    )
    return Candidate(regime=regime, spread=spread, rationale=rationale, notes=notes)
