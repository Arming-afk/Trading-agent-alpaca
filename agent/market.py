"""Assembling a usable option chain from two Alpaca endpoints.

Neither endpoint alone is enough, and this was worth finding out from the live
API rather than assuming:

* ``alpaca data option chain`` returns quotes, greeks and ``impliedVolatility``
  — but **no open interest**.
* ``alpaca option contracts`` returns open interest, the tradable flag and
  contract reference data — but **no quotes or greeks**.

Filtering on open interest against the chain endpoint alone therefore rejects
every contract, because they all arrive with an implicit zero. This module joins
the two by OCC symbol so the liquidity gate sees complete rows.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from agent import cli, config
from agent.chain import Contract, build_chain

logger = logging.getLogger(__name__)


def spot_price(symbol: str) -> float | None:
    """Reference price for the underlying: quote mid, else the last close.

    The fallback is not cosmetic. Outside regular hours one side of the NBBO is
    routinely withdrawn — AAPL quoted 300.93 bid / 0.00 ask at Friday's close —
    and requiring both sides makes the agent blind to a symbol for reasons that
    have nothing to do with whether it is worth trading. A one-sided quote still
    tells us where the stock is; the daily close tells us too.
    """
    try:
        quote = cli.latest_stock_quote(symbol)
    except cli.AlpacaCLIError as exc:
        logger.warning("spot quote failed for %s: %s", symbol, exc)
        quote = {}

    bid = float(quote.get("bp") or 0)
    ask = float(quote.get("ap") or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    if bid > 0 or ask > 0:
        return bid or ask

    closes = recent_closes(symbol, days=5)
    return closes[-1] if closes else None


def recent_closes(symbol: str, *, days: int = 60) -> list[float]:
    """Split-adjusted daily closes, oldest first — the realized-vol input."""
    start = (date.today() - timedelta(days=days * 2)).isoformat()
    try:
        bars = cli.stock_bars(symbol, start=start, timeframe="1Day", limit=days * 2)
    except cli.AlpacaCLIError as exc:
        logger.warning("bars failed for %s: %s", symbol, exc)
        return []
    closes = [float(b["c"]) for b in bars if b.get("c")]
    return closes[-days:] if len(closes) > days else closes


def open_interest_map(underlying: str, *, expiration_gte: str, expiration_lte: str,
                      option_type: str | None = None,
                      strike_gte: float | None = None,
                      strike_lte: float | None = None) -> dict[str, int]:
    """Symbol → open interest, skipping contracts Alpaca marks untradable."""
    try:
        rows = cli.option_contracts(
            underlying, expiration_gte=expiration_gte, expiration_lte=expiration_lte,
            contract_type=option_type, strike_gte=strike_gte, strike_lte=strike_lte,
            limit=10_000)
    except cli.AlpacaCLIError as exc:
        logger.warning("contract reference lookup failed for %s: %s", underlying, exc)
        return {}

    out: dict[str, int] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol or row.get("tradable") is False:
            continue
        try:
            out[symbol] = int(float(row.get("open_interest") or 0))
        except (TypeError, ValueError):
            out[symbol] = 0
    return out


def load_chain(underlying: str, *, spot: float, option_type: str,
               today: date | None = None,
               min_dte: int | None = None, max_dte: int | None = None,
               strike_band_pct: float = 12.0) -> list[Contract]:
    """A merged, quote-and-open-interest-complete chain for one underlying.

    Strikes are limited to a band around spot: the wings are irrelevant to a
    vertical placed near the money and fetching them only costs latency.
    """
    ref = today or date.today()
    lo_dte = config.MIN_DTE if min_dte is None else min_dte
    hi_dte = config.MAX_DTE if max_dte is None else max_dte
    gte = (ref + timedelta(days=lo_dte)).isoformat()
    lte = (ref + timedelta(days=hi_dte)).isoformat()

    band = strike_band_pct / 100
    strike_lo, strike_hi = spot * (1 - band), spot * (1 + band)

    try:
        raw = cli.option_chain(underlying, contract_type=option_type,
                               expiration_gte=gte, expiration_lte=lte,
                               strike_gte=strike_lo, strike_lte=strike_hi,
                               limit=1000)
    except cli.AlpacaCLIError as exc:
        logger.warning("chain fetch failed for %s: %s", underlying, exc)
        return []

    oi = open_interest_map(underlying, expiration_gte=gte, expiration_lte=lte,
                           option_type=option_type,
                           strike_gte=strike_lo, strike_lte=strike_hi)

    contracts = build_chain(raw, open_interest=oi)
    return [c for c in contracts if lo_dte <= c.dte(ref) <= hi_dte]
