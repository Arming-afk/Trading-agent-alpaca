"""Volatility measurement: what the options are pricing vs what the stock does.

The regime signal this agent trades on is the **variance risk premium** — the
gap between implied volatility and subsequent realized volatility. It is one of
the better-documented effects in options markets: index and large-cap options
have historically been priced above the volatility that actually arrives, which
is the compensation option sellers earn for carrying gap risk.

A note on what this is *not*. The textbook regime signal is IV rank, which
positions today's IV within its own 52-week range. That needs a year of IV
history the agent does not have and cannot backfill from a single chain call, so
using the IV/RV ratio here is a deliberate substitute, not a rename: it is
computable from one chain snapshot plus 20 days of bars, and it measures a
related but distinct thing. Both readings are recorded in the decision log.
"""
from __future__ import annotations

import math

from agent.chain import Contract

#: Trading days per year, for annualising a daily volatility.
ANNUALISATION = 252


def daily_returns(closes: list[float]) -> list[float]:
    """Log returns. Non-positive prices are dropped rather than propagating a
    domain error through the whole series."""
    clean = [c for c in closes if isinstance(c, (int, float)) and c > 0]
    return [math.log(clean[i] / clean[i - 1]) for i in range(1, len(clean))]


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """Annualised close-to-close volatility over the last `window` returns.

    Returns None rather than 0.0 when there is not enough data — a zero would
    silently become an infinite IV/RV ratio downstream.
    """
    returns = daily_returns(closes)
    if len(returns) < window or window < 2:
        return None
    sample = returns[-window:]
    mean = sum(sample) / len(sample)
    variance = sum((r - mean) ** 2 for r in sample) / (len(sample) - 1)
    return math.sqrt(variance) * math.sqrt(ANNUALISATION)


def atm_contract(contracts: list[Contract], spot: float) -> Contract | None:
    """The listed strike closest to spot."""
    priced = [c for c in contracts if c.mid > 0]
    if not priced or spot <= 0:
        return None
    return min(priced, key=lambda c: abs(c.strike - spot))


def atm_iv(contracts: list[Contract], spot: float) -> float | None:
    """Implied vol of the at-the-money contract, as a decimal (0.28 = 28%).

    Alpaca returns `impliedVolatility` alongside `greeks` in a chain snapshot
    rather than inside it, so Contract carries it directly.
    """
    atm = atm_contract(contracts, spot)
    if atm is None or not atm.implied_vol or atm.implied_vol <= 0:
        return None
    return atm.implied_vol


def variance_premium(implied: float | None, realized: float | None) -> float | None:
    """IV / RV. Above 1 means options are pricing more movement than the stock
    has recently delivered — the condition for selling premium."""
    if implied is None or realized is None or realized <= 0:
        return None
    return implied / realized


def trend_bias(closes: list[float], window: int = 20) -> str:
    """A deliberately blunt trend read: spot above or below its own moving
    average.

    This is not a forecast and is not claimed to have edge. Its only job is to
    pick which side of the market to sell premium on, so that the agent is not
    selling puts into a sustained decline. Any signal that answers that question
    consistently would do; this one is transparent and has no parameters worth
    tuning.
    """
    clean = [c for c in closes if isinstance(c, (int, float)) and c > 0]
    if len(clean) < window:
        return "neutral"
    sma = sum(clean[-window:]) / window
    spot = clean[-1]
    if spot > sma:
        return "bullish"
    if spot < sma:
        return "bearish"
    return "neutral"


# ── jump contamination ───────────────────────────────────────────────────────
# Why this exists, concretely. On 2026-08-31 the agent read NVDA at IV/RV 0.63
# and bought a debit spread on the claim that options were cheap. They were not.
# Realized vol was 45% because the trailing 20-day window contained a single
# earnings gap, while implied had already been crushed the day after that same
# event. The ratio was low because the *denominator* was contaminated, not
# because the surface was dislocated.
#
# A close-to-close estimator cannot tell a jump apart from volatility — that is
# the known weakness of the estimator, not a surprise. So rather than trusting
# one number, the agent computes the same window twice: once as-is, and once
# with the single largest absolute return removed. If one event is carrying the
# reading, the two disagree, and a signal that only survives on the contaminated
# reading is not traded.

#: A window is called jump-contaminated when removing its single largest
#: absolute return moves the volatility estimate by more than this fraction.
JUMP_TOLERANCE = 0.20


def _annualised(sample: list[float]) -> float | None:
    if len(sample) < 2:
        return None
    mean = sum(sample) / len(sample)
    variance = sum((r - mean) ** 2 for r in sample) / (len(sample) - 1)
    return math.sqrt(variance) * math.sqrt(ANNUALISATION)


def realized_vol_ex_jump(closes: list[float], window: int = 20) -> float | None:
    """Annualised close-to-close vol with the single largest absolute return
    dropped from the window.

    This is not a better estimate of volatility — it is deliberately biased
    low. Its only job is to be compared against the ordinary estimate, so that
    a window whose reading rests on one day can be identified as such.
    """
    returns = daily_returns(closes)
    if len(returns) < window or window < 3:
        return None
    sample = list(returns[-window:])
    sample.remove(max(sample, key=abs))
    return _annualised(sample)


def jump_ratio(closes: list[float], window: int = 20) -> float | None:
    """How much of the realized-vol reading rests on its single largest day,
    as a fraction. 0.0 means removing that day changes nothing; 0.45 means the
    estimate falls by 45% without it.
    """
    full = realized_vol(closes, window=window)
    trimmed = realized_vol_ex_jump(closes, window=window)
    if full is None or trimmed is None or full <= 0:
        return None
    return max((full - trimmed) / full, 0.0)


def is_jump_contaminated(closes: list[float], window: int = 20,
                         tolerance: float = JUMP_TOLERANCE) -> bool:
    """True when one day is carrying the realized-vol reading."""
    ratio = jump_ratio(closes, window=window)
    return ratio is not None and ratio > tolerance
