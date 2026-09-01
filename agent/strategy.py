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

# ── regime thresholds ────────────────────────────────────────────────────────
# Set a-priori from the documented behaviour of the variance risk premium, and
# corrected once — before any order was placed — when a dry run showed the first
# numbers firing on the ordinary case instead of the exceptional one.
#
# The premium is normally *positive*: implied has historically run a few vol
# points above subsequent realized, so a typical IV/RV reading sits above 1.0,
# not at it. The first pass used 0.95 as "cheap", which is roughly the middle of
# the normal range — six of eight symbols produced a trade, five of them debit
# spreads bought on the claim that premium was unusually cheap when it was
# merely unremarkable. Thresholds now sit outside the ordinary band on both
# sides, so a signal means the surface is genuinely dislocated.
#
# This is a calibration against published behaviour, not a fit to results: no
# order had been submitted when it was made, and it is not revisited on the
# strength of the competition's P&L.

#: Above this, implied vol is rich enough relative to realized to sell.
RICH_RATIO = 1.30
#: Below this, options are cheap enough relative to realized to buy.
CHEAP_RATIO = 0.85

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
    #: The same window with its single largest return removed, and the ratio
    #: that follows from it. Both are recorded whether or not they changed the
    #: decision — a reading that survived the robustness check is worth as much
    #: in the record as one that failed it.
    realized_vol_ex_jump: float | None = None
    ratio_ex_jump: float | None = None
    #: Fraction of the realized-vol reading carried by its largest single day.
    jump_ratio: float | None = None
    #: True when the two readings disagreed about what to do.
    jump_blocked: bool = False

    def as_log(self) -> dict:
        return {
            "symbol": self.symbol,
            "implied_vol": self.implied_vol,
            "realized_vol": self.realized_vol,
            "iv_rv_ratio": self.ratio,
            "realized_vol_ex_jump": self.realized_vol_ex_jump,
            "iv_rv_ratio_ex_jump": self.ratio_ex_jump,
            "jump_ratio": self.jump_ratio,
            "jump_blocked": self.jump_blocked,
            "trend_bias": self.bias,
            "stance": self.stance,
            "reason": self.reason,
        }


@dataclass
class Proposal:
    """The outcome of considering one symbol: a trade, or a stated reason there
    is none.

    Returning a bare None here was actively misleading in the journal. The
    runner had nothing to log but the regime's own text, so a symbol declined
    for having no directional view was recorded as "premium is cheap" — which
    is what the regime said, not what the agent did. The log is the audit trail
    the competition is judged against; it has to say the real reason.
    """
    candidate: "Candidate | None"
    reason: str

    def __bool__(self) -> bool:
        return self.candidate is not None


@dataclass
class Candidate:
    """A proposed trade, before risk sizing."""
    regime: Regime
    spread: Vertical
    rationale: str
    notes: list[str] = field(default_factory=list)
    #: Result of the earnings-calendar lookup, attached by the runner once the
    #: expiry is known. `agent.earnings.EventCheck | None`, kept untyped here so
    #: the strategy module does not depend on the calendar to be importable.
    event: object | None = None


def _stance_for(ratio: float | None, rich_ratio: float, cheap_ratio: float) -> str:
    if ratio is None:
        return STAND_ASIDE
    if ratio >= rich_ratio:
        return SELL_PREMIUM
    if ratio <= cheap_ratio:
        return BUY_PREMIUM
    return STAND_ASIDE


def classify(symbol: str, *, implied: float | None, realized: float | None,
             closes: list[float],
             rich_ratio: float = RICH_RATIO,
             cheap_ratio: float = CHEAP_RATIO,
             window: int = 20) -> Regime:
    """Map the IV/RV reading onto a stance. Missing data means stand aside.

    The reading is taken twice. Close-to-close realized volatility cannot tell
    a jump apart from volatility, so a single earnings gap inside the trailing
    window inflates the denominator and the ratio reads low — options look
    cheap when what actually happened is that the yardstick broke. That is not
    hypothetical: it is exactly how NVDA came back at IV/RV 0.63 on 2026-08-31
    off a 45% realized reading, and the agent bought a debit spread on it.

    So the same window is measured again with its single largest return
    removed, and **a stance has to survive both readings**. If dropping one day
    moves the symbol into a different bucket, the signal was that one day, and
    the agent stands aside. The check is symmetric by construction: it can only
    ever remove a trade, never create one.
    """
    ratio = vol.variance_premium(implied, realized)
    bias = vol.trend_bias(closes)
    jump = vol.jump_ratio(closes, window=window)

    # The ex-jump reading is derived by *scaling* the realized vol the caller
    # supplied, not by recomputing an absolute one from the closes. They are
    # usually the same number, but only usually — the caller owns the choice of
    # estimator and window, and an independently computed second figure would
    # silently compare two different measurements whenever they diverged. What
    # is wanted here is one quantity: how far this reading falls when its
    # largest day is dropped.
    realized_ex_jump = (realized * (1 - jump)
                        if realized is not None and jump is not None else None)
    ratio_ex_jump = vol.variance_premium(implied, realized_ex_jump)

    stance = _stance_for(ratio, rich_ratio, cheap_ratio)
    jump_blocked = False

    if ratio is None:
        reason = "no IV/RV reading (missing chain greeks or bars)"
    elif stance == SELL_PREMIUM:
        reason = f"IV/RV {ratio:.2f} >= {rich_ratio:.2f} — premium is rich"
    elif stance == BUY_PREMIUM:
        reason = f"IV/RV {ratio:.2f} <= {cheap_ratio:.2f} — premium is cheap"
    else:
        reason = f"IV/RV {ratio:.2f} inside [{cheap_ratio:.2f}, {rich_ratio:.2f}] — no edge claimed"

    # Two independent conditions, because they fail at different places.
    #
    # The stance-flip test alone is too coarse near a threshold, and the first
    # live run proved it within hours: NVDA came back at IV/RV 0.635 with 24.9%
    # of its realized vol carried by one session, and the ex-jump ratio landed
    # at 0.846 against a 0.85 cheap threshold. The stance did not flip — by
    # four thousandths — so a reading that was a quarter one earnings gap was
    # about to be traded as a statement about the volatility surface.
    #
    # So a contaminated window is refused on its own terms. The question the
    # ratio is supposed to answer is whether implied is dislocated against what
    # the stock has been doing; when a quarter of "what the stock has been
    # doing" is a single event that has already happened, the ratio is not
    # measuring that, and how close it lands to a threshold is beside the point.
    if stance != STAND_ASIDE and jump is not None and jump > vol.JUMP_TOLERANCE:
        jump_blocked = True
        stance = STAND_ASIDE
        reason = (f"IV/RV {ratio:.2f} rests on one session — {jump:.0%} of "
                  f"realized vol is a single day (ex-jump {ratio_ex_jump:.2f}); "
                  f"the denominator is an event, not a volatility reading")
    elif stance != STAND_ASIDE and ratio_ex_jump is not None:
        robust = _stance_for(ratio_ex_jump, rich_ratio, cheap_ratio)
        if robust != stance:
            jump_blocked = True
            stance = STAND_ASIDE
            reason = (
                f"IV/RV {ratio:.2f} does not survive dropping the largest day "
                f"({ratio_ex_jump:.2f} ex-jump, {jump:.0%} of realized vol was "
                f"one session) — the reading is the event, not the surface"
                if jump is not None else
                f"IV/RV {ratio:.2f} does not survive dropping the largest day "
                f"({ratio_ex_jump:.2f} ex-jump)"
            )

    return Regime(symbol=symbol, implied_vol=implied, realized_vol=realized,
                  ratio=ratio, bias=bias, stance=stance, reason=reason,
                  realized_vol_ex_jump=realized_ex_jump,
                  ratio_ex_jump=ratio_ex_jump, jump_ratio=jump,
                  jump_blocked=jump_blocked)


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


def consider(regime: Regime, contracts: list[Contract], spot: float,
             *, today: date | None = None) -> Proposal:
    """Turn a regime reading into a concrete, priced spread — or a stated reason
    there is none. Declining is a normal outcome, not an error."""
    if regime.stance == STAND_ASIDE:
        return Proposal(None, regime.reason)

    kind = spread_kind_for(regime.stance, regime.bias)
    if kind is None:
        return Proposal(None, "buying premium needs a directional view; trend is neutral")

    tradable = [c for c in contracts if is_tradable(c, today=today)]
    if not tradable:
        return Proposal(None, f"no contract passed the liquidity gate ({len(contracts)} seen)")

    option_type = _option_type_for(kind)
    expiry = best_expiry([c for c in tradable if c.kind == option_type],
                         TARGET_DTE, today=today)
    if expiry is None:
        return Proposal(None, f"no {option_type} expiry with enough liquid strikes")

    legs = select_legs(kind, tradable, spot, expiration=expiry, today=today)
    if legs is None:
        return Proposal(None, f"could not place two strikes on {expiry}")

    try:
        spread = spreads.build(kind, list(legs))
    except ValueError as exc:
        return Proposal(None, f"spread rejected: {exc}")

    # A credit spread that collects nothing carries the full width as risk for
    # no compensation; a debit spread priced at the full width has no upside.
    if spread.max_gain <= 0 or spread.max_loss <= 0:
        return Proposal(None, "spread is priced with no upside or no risk — bad quotes")
    if spread.kind in spreads.CREDIT_KINDS and spread.reward_risk < MIN_CREDIT_REWARD_RISK:
        # Not a warning. Collecting $1 against $99 of risk is a losing structure
        # however often it expires worthless.
        return Proposal(None,
                        f"credit too thin: reward/risk {spread.reward_risk:.2f} "
                        f"< {MIN_CREDIT_REWARD_RISK:.2f}")

    notes = []
    if spread.worst_spread_pct > 15:
        notes.append(f"wide market on a leg: {spread.worst_spread_pct:.1f}%")

    rationale = (
        f"{regime.reason}; trend {regime.bias} → {kind} "
        f"{spread.long_leg.strike:g}/{spread.short_leg.strike:g} "
        f"{spread.long_leg.expiration:%Y-%m-%d}"
    )
    return Proposal(Candidate(regime=regime, spread=spread, rationale=rationale,
                              notes=notes), rationale)


def propose(regime: Regime, contracts: list[Contract], spot: float,
            *, today: date | None = None) -> Candidate | None:
    """`consider()` without the reason — kept for callers that only need the
    trade."""
    return consider(regime, contracts, spot, today=today).candidate
