"""Option chain handling: parse contracts, filter for liquidity, pick strikes.

The liquidity filters here matter more than they look. On a defined-risk
vertical you pay the bid/ask spread twice on the way in and twice on the way
out. A 10%-wide market on both legs can eat the entire theoretical edge of the
trade before the underlying moves at all, so an illiquid contract is rejected
outright rather than sized down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from agent import config

#: OCC symbol: root (1-6 chars) + YYMMDD + C|P + strike in thousandths (8 digits)
_OCC = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class Contract:
    """One option contract, with whatever market data we have for it."""
    symbol: str
    underlying: str
    expiration: date
    strike: float
    kind: str            # "call" | "put"
    bid: float = 0.0
    ask: float = 0.0
    delta: float | None = None
    open_interest: int = 0

    @property
    def mid(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        """Bid/ask width as a percentage of mid. inf when there is no market."""
        m = self.mid
        if m <= 0:
            return float("inf")
        return (self.ask - self.bid) / m * 100

    def dte(self, today: date | None = None) -> int:
        return (self.expiration - (today or date.today())).days


def parse_occ(symbol: str) -> tuple[str, date, str, float]:
    """Split an OCC symbol into (underlying, expiration, kind, strike)."""
    m = _OCC.match(symbol.strip().upper())
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    ymd = m.group("ymd")
    expiration = datetime.strptime(ymd, "%y%m%d").date()
    kind = "call" if m.group("cp") == "C" else "put"
    strike = int(m.group("strike")) / 1000
    return m.group("root"), expiration, kind, strike


def from_snapshot(symbol: str, snapshot: dict, *, open_interest: int = 0) -> Contract:
    """Build a Contract from one entry of `alpaca data option chain`."""
    underlying, expiration, kind, strike = parse_occ(symbol)
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    greeks = snapshot.get("greeks") or {}
    return Contract(
        symbol=symbol,
        underlying=underlying,
        expiration=expiration,
        strike=strike,
        kind=kind,
        bid=float(quote.get("bp") or quote.get("bid_price") or 0.0),
        ask=float(quote.get("ap") or quote.get("ask_price") or 0.0),
        delta=(float(greeks["delta"]) if greeks.get("delta") is not None else None),
        open_interest=open_interest,
    )


def build_chain(snapshots: dict[str, dict],
                open_interest: dict[str, int] | None = None) -> list[Contract]:
    """Turn a raw chain payload into Contracts, skipping unparseable symbols."""
    oi = open_interest or {}
    out = []
    for symbol, snap in snapshots.items():
        try:
            out.append(from_snapshot(symbol, snap, open_interest=oi.get(symbol, 0)))
        except (ValueError, TypeError):
            continue
    return out


def is_tradable(c: Contract, *, today: date | None = None,
                max_spread_pct: float | None = None,
                min_open_interest: int | None = None,
                min_dte: int | None = None,
                max_dte: int | None = None) -> bool:
    """Hard liquidity gate. A contract that fails this is never traded, at any
    size — see the module docstring."""
    max_spread = config.MAX_SPREAD_PCT if max_spread_pct is None else max_spread_pct
    min_oi = config.MIN_OPEN_INTEREST if min_open_interest is None else min_open_interest
    lo = config.MIN_DTE if min_dte is None else min_dte
    hi = config.MAX_DTE if max_dte is None else max_dte

    if c.bid <= 0 or c.ask <= 0 or c.ask < c.bid:
        return False
    if max_spread > 0 and c.spread_pct > max_spread:
        return False
    if min_oi > 0 and c.open_interest < min_oi:
        return False
    d = c.dte(today)
    return lo <= d <= hi


def expiries(contracts: list[Contract]) -> list[date]:
    return sorted({c.expiration for c in contracts})


def nearest_expiry(contracts: list[Contract], target_dte: int,
                   today: date | None = None) -> date | None:
    """The listed expiry closest to target_dte."""
    available = expiries(contracts)
    if not available:
        return None
    ref = today or date.today()
    return min(available, key=lambda e: abs((e - ref).days - target_dte))


def by_delta(contracts: list[Contract], target_delta: float) -> Contract | None:
    """Contract whose |delta| is closest to target. Falls back to None when the
    chain carries no greeks — the caller then picks by strike instead."""
    with_greeks = [c for c in contracts if c.delta is not None]
    if not with_greeks:
        return None
    return min(with_greeks, key=lambda c: abs(abs(c.delta) - abs(target_delta)))


def by_strike(contracts: list[Contract], target_strike: float) -> Contract | None:
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(c.strike - target_strike))
