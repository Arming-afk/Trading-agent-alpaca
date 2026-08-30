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

#: Strike distance between the legs, as a percentage of spot. The raw listed
#: increment is the wrong default: SPY lists $1 strikes on a ~$770 underlying,
#: so a one-increment spread risks the full width to collect a few cents. Width
#: has to scale with the price of the underlying, not with the strike grid.
SPREAD_WIDTH_PCT = 1.0

#: An expiry needs at least this many liquid strikes to be worth trading —
#: fewer means strike selection is choosing among whatever survived, not among
#: what it wants.
MIN_STRIKES_PER_EXPIRY = 6

#: A credit spread must pay at least this fraction of its risk. Below it the
#: structure is collecting pennies in front of the full width — the shape of
#: trade that wins repeatedly and then gives it all back at once.
MIN_CREDIT_REWARD_RISK = 0.15

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
                expiration: date | None = None,
                today: date | None = None) -> tuple[Contract, Contract] | None:
    """Pick the two strikes for `kind` from an already liquidity-filtered chain.

    Both legs are drawn from a **single expiry**. This is not a detail: a liquid
    underlying lists several expiries inside any DTE window, and picking the
    anchor by delta across all of them lands on one expiry while the protective
    strike search lands on another. The result is not a vertical at all — the
    legs do not offset, so `max_loss` would be a fiction and every risk gate
    downstream would be sizing against a number that does not describe the
    position. `spreads.Vertical` rejects mismatched expirations for the same
    reason; this filter is what stops that rejection from being the normal path.

    The anchor leg is chosen by delta when the chain carries greeks and by
    moneyness otherwise, so a chain without greeks degrades to a wider but still
    valid selection instead of failing outright.
    """
    option_type = _option_type_for(kind)
    typed = [c for c in contracts if c.kind == option_type]
    if len(typed) < 2 or spot <= 0:
        return None

    expiry = expiration or best_expiry(typed, TARGET_DTE, today=today)
    if expiry is None:
        return None
    pool = [c for c in typed if c.expiration == expiry]
    if len(pool) < 2:
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
    grid = _strike_increment(strikes)
    if grid <= 0:
        return None
    if width_target is not None:
        increment = width_target
    else:
        # Scale the width to the underlying, then snap it to the listed grid.
        wanted = spot * SPREAD_WIDTH_PCT / 100
        increment = max(grid, round(wanted / grid) * grid)
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


def best_expiry(contracts: list[Contract], target_dte: int = TARGET_DTE,
                today: date | None = None,
                min_strikes: int = MIN_STRIKES_PER_EXPIRY) -> date | None:
    """The expiry to trade: nearest to `target_dte` among those with enough
    liquid strikes to choose from.

    Proximity alone is the wrong rule. When no listed expiry sits near the
    target, the nearest one can be a thin weekly with a handful of surviving
    strikes, and strike selection then picks the least-bad of those rather than
    the one it actually wants. Requiring depth first keeps the choice on the
    monthlies, where the markets are.
    """
    counts: dict[date, int] = {}
    for c in contracts:
        counts[c.expiration] = counts.get(c.expiration, 0) + 1
    if not counts:
        return None
    ref = today or date.today()
    deep = [e for e, n in counts.items() if n >= min_strikes]
    pool = deep or list(counts)
    return min(pool, key=lambda e: abs((e - ref).days - target_dte))


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
    option_type = _option_type_for(kind)
    expiry = best_expiry([c for c in tradable if c.kind == option_type],
                         TARGET_DTE, today=today)
    if expiry is None:
        return None
    legs = select_legs(kind, tradable, spot, expiration=expiry, today=today)
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
    if spread.kind in spreads.CREDIT_KINDS and spread.reward_risk < MIN_CREDIT_REWARD_RISK:
        # Not a warning. Collecting $1 against $99 of risk is a losing structure
        # however often it expires worthless.
        return None
    if spread.worst_spread_pct > 15:
        notes.append(f"wide market on a leg: {spread.worst_spread_pct:.1f}%")

    rationale = (
        f"{regime.reason}; trend {regime.bias} → {kind} "
        f"{spread.long_leg.strike:g}/{spread.short_leg.strike:g} "
        f"{spread.long_leg.expiration:%Y-%m-%d}"
    )
    return Candidate(regime=regime, spread=spread, rationale=rationale, notes=notes)
