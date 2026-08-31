#!/usr/bin/env python3
"""One scheduled pass of the agent.

Sequence, in order, with the reason each step comes where it does:

1. **Market clock.** Options quotes outside RTH are stale and multi-leg orders
   will not fill, so a closed market ends the run before anything else happens.
2. **Account and history.** Equity, open positions and the equity curve — the
   risk gates need all three and none of them are worth computing per symbol.
3. **Manage what is already open**, before opening anything new. A spread that
   should be closed frees both risk budget and a position slot, and doing this
   second would size new trades against stale exposure.
4. **Survey the universe.** Every symbol gets a regime reading, and every
   symbol's outcome is logged — including the refusals, which are the majority.
5. **Rank, gate, size, submit.** Candidates compete; the gates run per trade
   because each fill changes the exposure the next one is measured against.

Run it with --dry-run to do all of the above and submit nothing.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import chain as ch
from agent import cli, config, journal, market, risk, spreads, strategy, vol

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_run")

#: Close a credit spread once this fraction of the premium has decayed. Taking
#: profit early on a short-premium position is not timidity: the last 25% of the
#: credit takes the longest to arrive and carries the same tail risk throughout.
PROFIT_TARGET = 0.60
#: Close anything with fewer than this many days left. Gamma near expiry turns a
#: defined-risk spread into a coin flip on the last move.
CLOSE_BEFORE_DTE = 5


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="One scheduled pass of the options agent.")
    p.add_argument("--dry-run", action="store_true",
                   help="decide and log everything, submit nothing")
    p.add_argument("--force", action="store_true",
                   help="run even when the market is closed (selection only)")
    p.add_argument("--universe", default=None,
                   help="comma-separated symbols, overriding UNIVERSE")
    p.add_argument("--max-contracts", type=int, default=None,
                   help="hard cap on contracts per spread, below whatever the "
                        "risk budget allows — for a supervised first live run")
    p.add_argument("--aggression", type=float, default=0.5,
                   help="0 prices at mid, 1 crosses both markets (default 0.5)")
    return p.parse_args(argv)


def survey(symbol: str, today: date) -> tuple[strategy.Regime | None, list, float | None]:
    """Regime reading plus the chain it was read from. Never raises: a symbol
    that cannot be read is a symbol the agent stands aside on."""
    try:
        spot = market.spot_price(symbol)
        if not spot:
            return None, [], None
        closes = market.recent_closes(symbol, days=60)
        realized = vol.realized_vol(closes, window=20)

        # IV is read from the put surface — one fetch, and near the money the
        # two sides are close enough for a regime reading.
        puts = market.load_chain(symbol, spot=spot, option_type="put", today=today)
        liquid = [c for c in puts if ch.is_tradable(c, today=today)]
        implied = vol.atm_iv(liquid, spot)

        regime = strategy.classify(symbol, implied=implied, realized=realized,
                                   closes=closes)

        # Which side we need depends on the structure, not on the stance alone:
        # selling premium in a downtrend sells calls, and buying it in a
        # downtrend buys puts. Deciding from the stance alone handed a bearish
        # buy-premium symbol a chain of calls and then declined it for having
        # no put expiry.
        kind = strategy.spread_kind_for(regime.stance, regime.bias)
        if kind is None:
            return regime, liquid, spot
        needed = "put" if kind in (spreads.BULL_PUT_CREDIT,
                                   spreads.BEAR_PUT_DEBIT) else "call"
        if needed == "call":
            calls = market.load_chain(symbol, spot=spot, option_type="call",
                                      today=today)
            liquid = [c for c in calls if ch.is_tradable(c, today=today)]
        return regime, liquid, spot
    except cli.AlpacaCLIError as exc:
        logger.warning("%s: survey failed — %s", symbol, exc)
        return None, [], None


def manage_open_positions(positions: list[dict], *, dry_run: bool,
                          today: date) -> list[dict]:
    """Close spreads that have hit their profit target or are near expiry.

    Alpaca reports option positions leg by leg, so this works on legs directly
    rather than trying to reconstruct the spread: any leg inside the expiry
    window is closed, and a defined-risk spread whose legs both close is flat.
    """
    closed = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        try:
            _, expiration, _, _ = ch.parse_occ(symbol)
        except ValueError:
            continue                      # equity position; not ours to manage

        dte = (expiration - today).days
        qty = abs(int(float(pos.get("qty") or 0)))
        if qty == 0:
            continue

        plpc = float(pos.get("unrealized_plpc") or 0)
        reason = None
        if dte <= CLOSE_BEFORE_DTE:
            reason = f"{dte} DTE — inside the gamma window"
        elif plpc >= PROFIT_TARGET:
            reason = f"unrealized {plpc:.0%} — profit target"
        if reason is None:
            continue

        logger.info("closing %s: %s", symbol, reason)
        if dry_run or not config.TRADING_ENABLED:
            journal.log_decision(symbol=symbol, action="declined", regime={},
                                 reason=f"would close ({reason}) — submission disabled")
            continue
        try:
            order = cli.run("position", "close", symbol)
            journal.log_decision(symbol=symbol, action="closed", regime={},
                                 order=order, reason=reason,
                                 command=cli.as_argv("position", "close", symbol))
            closed.append(order)
        except cli.AlpacaCLIError as exc:
            journal.log_decision(symbol=symbol, action="error", regime={},
                                 reason=f"close failed: {exc}")
    return closed


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()
    dry_run = args.dry_run or not config.TRADING_ENABLED
    universe = ([s.strip().upper() for s in args.universe.split(",") if s.strip()]
                if args.universe else config.UNIVERSE)

    if dry_run:
        logger.info("DRY RUN — decisions will be logged, no orders submitted")

    try:
        clock = cli.clock()
    except cli.AlpacaCLIError as exc:
        logger.error("cannot reach Alpaca: %s", exc)
        return 1

    if not clock.get("is_open") and not args.force:
        logger.info("market closed — nothing to do")
        journal.log_run({"date": str(today), "status": "market_closed"})
        return 0

    account = cli.account()
    equity = float(account.get("equity") or 0)
    positions = cli.positions()
    history = journal.equity_history()
    hwm = risk.high_water_mark(history, current=equity)

    logger.info("equity %.2f | high-water %.2f | %d open legs",
                equity, hwm or 0, len(positions))

    closed = manage_open_positions(positions, dry_run=dry_run, today=today)
    if closed:
        positions = cli.positions()

    # Open risk is recomputed from what the broker reports rather than from the
    # journal: the broker is authoritative about what is actually on.
    open_risk = sum(abs(float(p.get("cost_basis") or 0)) for p in positions)
    trades_today = journal.trades_opened_today(str(today))

    candidates = []
    for symbol in universe:
        regime, contracts, spot = survey(symbol, today)
        if regime is None:
            journal.log_decision(symbol=symbol, action="declined", regime={},
                                 reason="no market data")
            continue
        proposal = strategy.consider(regime, contracts, spot or 0.0, today=today)
        if not proposal:
            journal.log_decision(symbol=symbol, action="declined",
                                 regime=regime.as_log(), reason=proposal.reason)
            continue
        candidates.append(proposal.candidate)

    # Best reward per unit of risk first — with defined risk on both sides that
    # ratio is directly comparable across symbols and structures.
    candidates.sort(key=lambda c: c.spread.reward_risk, reverse=True)
    logger.info("%d candidates from %d symbols", len(candidates), len(universe))

    opened = 0
    for cand in candidates:
        verdict = risk.evaluate(equity=equity, high_water_mark=hwm,
                                open_risk=open_risk,
                                trades_today=trades_today + opened)
        risk_log = {"allowed": verdict.allowed, "budget": round(verdict.risk_budget, 2),
                    "blocked_by": verdict.blocked_by, "detail": verdict.reason(),
                    "open_risk": round(open_risk, 2)}
        if args.max_contracts is not None:
            risk_log["max_contracts_cap"] = args.max_contracts
        if not verdict.allowed:
            journal.log_decision(symbol=cand.regime.symbol, action="declined",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(cand.spread),
                                 risk=risk_log, reason=verdict.reason())
            continue

        qty = spreads.size_for_risk(cand.spread, verdict.risk_budget)
        if args.max_contracts is not None:
            # A cap can only ever shrink the position: the risk budget stays
            # authoritative, this just refuses to use all of it.
            qty = min(qty, args.max_contracts)
        if qty < 1:
            journal.log_decision(
                symbol=cand.regime.symbol, action="declined",
                regime=cand.regime.as_log(),
                spread=journal.spread_snapshot(cand.spread), risk=risk_log,
                reason=f"one contract risks more than the ${verdict.risk_budget:.0f} budget")
            continue

        sized = spreads.Vertical(cand.spread.kind, cand.spread.long_leg,
                                 cand.spread.short_leg, qty=qty)
        limit = sized.limit_price(args.aggression)
        legs = sized.legs_payload()
        command = cli.as_argv("order", "submit", "--order-class", "mleg",
                              "--type", "limit", "--qty", str(qty),
                              "--limit-price", f"{limit:.2f}",
                              "--time-in-force", "day", "--legs", "<legs.json>")

        logger.info("%s → %s | mid %.2f natural %.2f limit %.2f",
                    cand.regime.symbol, sized.describe(),
                    abs(sized.net_mid), sized.natural_price, limit)

        if dry_run:
            journal.log_decision(symbol=cand.regime.symbol, action="declined",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(sized),
                                 risk=risk_log, command=command,
                                 notes=cand.notes,
                                 reason="dry run — not submitted")
            continue

        try:
            order = cli.submit_mleg(legs, qty=qty, limit_price=limit)
        except cli.AlpacaCLIError as exc:
            journal.log_decision(symbol=cand.regime.symbol, action="error",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(sized),
                                 risk=risk_log, command=command,
                                 reason=f"submit rejected: {exc}")
            continue

        journal.log_decision(symbol=cand.regime.symbol, action="opened",
                             regime=cand.regime.as_log(),
                             spread=journal.spread_snapshot(sized),
                             risk=risk_log, order=order, command=command,
                             notes=cand.notes, reason=cand.rationale)
        opened += 1
        open_risk += sized.max_loss

    account_after = cli.account()
    journal.log_run({
        "date": str(today),
        "status": "dry_run" if dry_run else "traded",
        "equity_before": equity,
        "equity_after": float(account_after.get("equity") or 0),
        "cash": float(account_after.get("cash") or 0),
        "high_water_mark": hwm,
        "open_legs": len(cli.positions()),
        "candidates": len(candidates),
        "opened": opened,
        "closed": len(closed),
        "universe": universe,
    })
    logger.info("done — %d opened, %d closed", opened, len(closed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
