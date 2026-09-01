#!/usr/bin/env python3
"""One scheduled pass of the agent.

Sequence, in order, with the reason each step comes where it does:

1. **Market clock.** Options quotes outside RTH are stale and multi-leg orders
   will not fill, so a closed market ends the run before anything else happens.
2. **Account and history.** Equity, open positions and the equity curve — the
   risk gates need all three and none of them are worth computing per symbol.
3. **Reconcile.** Alpaca reports option positions leg by leg; the spreads they
   belong to exist only in this agent's journal. `agent/positions.py` joins the
   two, which is what makes open risk and profit-taking answerable at all.
4. **Manage what is already open**, before opening anything new. A spread that
   should be closed frees both risk budget and a position slot, and doing this
   second would size new trades against stale exposure.
5. **Survey the universe.** Every symbol gets a regime reading, and every
   symbol's outcome is logged — including the refusals, which are the majority.
6. **Rank, gate, size, submit.** Candidates compete; the gates run per trade
   because each fill changes the exposure the next one is measured against.
7. **Chase.** A limit sent once is a bid, not an execution strategy. Unfilled
   packages are re-quoted toward the market — and re-sized at every step, since
   a worse price is more risk.

## Two modes, because the schedule is not reliable

GitHub's cron is best-effort. On 2026-08-31 the 14:00 UTC trigger arrived at
19:43, seventeen minutes before the close, which is why two of that day's three
orders never filled. On 2026-09-01 it did not arrive at all and the agent
traded nothing.

The answer is to run often and let the first pass that lands do the work:

* a **full** run surveys the universe and may open positions;
* a **maintenance** run happens when a full run has already completed today.
  It reconciles, manages open positions and chases unfilled orders, but opens
  nothing new.

Both write a run record. `--mode` overrides the choice.

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
from agent import (advisor as advisor_mod, cli, config, earnings, execution,
                   journal, market, positions as pos_mod, risk, spreads,
                   strategy, vol)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_run")

#: Close a spread once this fraction of its **maximum profit** has been earned.
#: Measured on the package, never on a leg: on a credit spread the short leg
#: reaches +60% of its own cost long before the position reaches 60% of max
#: gain, and closing that leg alone strands the long one.
PROFIT_TARGET = 0.60
#: Close anything with fewer than this many days left. Gamma near expiry turns a
#: defined-risk spread into a coin flip on the last move.
CLOSE_BEFORE_DTE = 5

FULL = "full"
MAINTENANCE = "maintenance"


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
    p.add_argument("--mode", choices=[FULL, MAINTENANCE, "auto"], default="auto",
                   help="auto (default) runs a full survey unless one already "
                        "completed today, then falls back to maintenance")
    p.add_argument("--chase-rounds", type=int, default=execution.DEFAULT_ROUNDS,
                   help="how many times to re-quote an unfilled order (0 = off)")
    p.add_argument("--chase-wait", type=int, default=execution.DEFAULT_WAIT,
                   help="seconds to leave an order resting between re-quotes")
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


def manage_open_positions(open_spreads: list[pos_mod.OpenSpread], *,
                          dry_run: bool, today: date) -> list[dict]:
    """Close spreads that have hit their profit target or are near expiry.

    Works on reconstructed spreads, not on raw legs. The previous version read
    `unrealized_plpc` off each leg and closed whichever leg crossed 60% — which
    is not the spread's profit target, and which turned a defined-risk position
    into a stranded single option on the way past it.
    """
    closed = []
    for spread in open_spreads:
        if spread.state == pos_mod.ORPHAN:
            # Never opened by this agent, or left behind by a manual action.
            # It is counted in open risk at its own worst case and reported —
            # but automatically flattening a position the agent does not
            # understand is how a bug becomes a market order.
            journal.log_decision(
                symbol=spread.underlying, action="declined", regime={},
                reason="orphan leg at the broker — counted in open risk, "
                       "not closed automatically; needs a human",
                spread=spread.as_log())
            continue

        dte = spread.dte(today)
        fraction = spread.profit_fraction
        reason = None
        if dte is not None and dte <= CLOSE_BEFORE_DTE:
            reason = f"{dte} DTE — inside the gamma window"
        elif fraction is not None and fraction >= PROFIT_TARGET:
            reason = (f"package at {fraction:.0%} of max gain "
                      f"(${spread.unrealized_pl:,.0f} of ${spread.max_gain:,.0f}) "
                      f"— profit target")
        if reason is None:
            continue

        logger.info("closing %s: %s", spread.describe(), reason)
        if dry_run or not config.TRADING_ENABLED:
            journal.log_decision(symbol=spread.underlying, action="declined",
                                 regime={}, spread=spread.as_log(),
                                 reason=f"would close ({reason}) — submission disabled")
            continue

        result = execution.close_spread(spread, reason=reason)
        if not result.get("submitted"):
            journal.log_decision(symbol=spread.underlying, action="error",
                                 regime={}, spread=spread.as_log(),
                                 reason=f"close failed: {result.get('why')}")
            continue

        journal.log_decision(symbol=spread.underlying, action="closed", regime={},
                             spread=spread.as_log(), order=result["order"],
                             command=result.get("command"), reason=reason)
        closed.append(result)
    return closed


def _event_check(symbol: str, expiration: date, today: date) -> earnings.EventCheck:
    return earnings.check(symbol, today=today, expiration=expiration)


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()
    dry_run = args.dry_run or not config.TRADING_ENABLED
    universe = ([s.strip().upper() for s in args.universe.split(",") if s.strip()]
                if args.universe else config.UNIVERSE)

    mode = args.mode
    if mode == "auto":
        mode = MAINTENANCE if journal.completed_full_run_today(str(today)) else FULL

    if dry_run:
        logger.info("DRY RUN — decisions will be logged, no orders submitted")
    logger.info("mode: %s", mode)

    try:
        clock = cli.clock()
    except cli.AlpacaCLIError as exc:
        logger.error("cannot reach Alpaca: %s", exc)
        return 1

    if not clock.get("is_open") and not args.force:
        logger.info("market closed — nothing to do")
        journal.log_run({"date": str(today), "status": "market_closed", "mode": mode})
        return 0

    account = cli.account()
    equity = float(account.get("equity") or 0)
    broker_legs = cli.positions()
    history = journal.equity_history()
    hwm = risk.high_water_mark(history, current=equity)

    # ── reconcile ────────────────────────────────────────────────────────────
    open_spreads, unexplained = pos_mod.reconcile(broker_legs)
    open_risk = pos_mod.open_risk(open_spreads)
    logger.info("equity %.2f | high-water %.2f | %d legs → %d spreads | open risk %.2f",
                equity, hwm or 0, len(broker_legs), len(open_spreads), open_risk)
    for item in unexplained:
        logger.warning("unexplained position: %s", item.describe())

    closed = manage_open_positions(open_spreads, dry_run=dry_run, today=today)
    if closed:
        open_spreads, unexplained = pos_mod.reconcile(cli.positions())
        open_risk = pos_mod.open_risk(open_spreads)

    trades_today = journal.trades_opened_today(str(today))
    advisor = advisor_mod.Advisor.from_config()
    candidates: list[strategy.Candidate] = []

    # ── survey ───────────────────────────────────────────────────────────────
    if mode == FULL:
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

            # Event risk is checked once the expiry is known, because that is
            # what the question is about: whether a scheduled binary event
            # falls inside the holding period this particular spread creates.
            cand = proposal.candidate
            event = _event_check(symbol, cand.spread.long_leg.expiration, today)
            if event.blocked:
                journal.log_decision(symbol=symbol, action="declined",
                                     regime=regime.as_log(),
                                     spread=journal.spread_snapshot(cand.spread),
                                     reason=f"event risk: {event.reason}",
                                     notes=[f"earnings: {event.status}"])
                continue
            cand.notes.append(f"earnings calendar: {event.status} — {event.reason}")
            cand.event = event
            candidates.append(cand)
    else:
        logger.info("maintenance pass — a full survey already completed today; "
                    "managing and chasing only")

    # Best reward per unit of risk first — with defined risk on both sides that
    # ratio is directly comparable across symbols and structures.
    candidates.sort(key=lambda c: c.spread.reward_risk, reverse=True)
    logger.info("%d candidates from %d symbols", len(candidates), len(universe))

    opened = 0
    pending: list[execution.Pending] = []
    for cand in candidates:
        symbol = cand.regime.symbol
        verdict = risk.evaluate(equity=equity, high_water_mark=hwm,
                                open_risk=open_risk,
                                trades_today=trades_today + opened)
        risk_log = {"allowed": verdict.allowed, "budget": round(verdict.risk_budget, 2),
                    "blocked_by": verdict.blocked_by, "detail": verdict.reason(),
                    "open_risk": round(open_risk, 2)}
        if args.max_contracts is not None:
            risk_log["max_contracts_cap"] = args.max_contracts
        if not verdict.allowed:
            journal.log_decision(symbol=symbol, action="declined",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(cand.spread),
                                 risk=risk_log, reason=verdict.reason())
            continue

        # Price first, then size. The limit is the price the package will
        # actually trade at; the midpoint is a quote. Sizing on the midpoint and
        # submitting at the limit spends the concession out of a budget no gate
        # granted — on 2026-08-31 that put NVDA at 2.10% of equity against a 2%
        # cap. See spreads.Vertical.max_loss_at.
        limit = cand.spread.limit_price(args.aggression)
        qty = spreads.size_for_risk(cand.spread, verdict.risk_budget, net_price=limit)
        if args.max_contracts is not None:
            # A cap can only ever shrink the position: the risk budget stays
            # authoritative, this just refuses to use all of it.
            qty = min(qty, args.max_contracts)
        if qty < 1:
            journal.log_decision(
                symbol=symbol, action="declined", regime=cand.regime.as_log(),
                spread=journal.spread_snapshot(cand.spread), risk=risk_log,
                reason=f"one contract at the {limit:.2f} limit risks more than "
                       f"the ${verdict.risk_budget:.0f} budget")
            continue

        sized = spreads.Vertical(cand.spread.kind, cand.spread.long_leg,
                                 cand.spread.short_leg, qty=qty)
        risk_at_limit = sized.max_loss_at(limit)
        risk_log["risk_at_limit"] = round(risk_at_limit, 2)
        legs = sized.legs_payload()
        command = cli.as_argv("order", "submit", "--order-class", "mleg",
                              "--type", "limit", "--qty", str(qty),
                              "--limit-price", f"{limit:.2f}",
                              "--time-in-force", "day", "--legs", "<legs.json>")

        # ── the advisor, last and least ──────────────────────────────────────
        # It runs after every gate has passed, and it can only subtract. See
        # agent/advisor.py for why the authority runs in that direction.
        brief = advisor_mod.brief_for(cand, event_check=getattr(cand, "event", None),
                                      jump_ratio=cand.regime.jump_ratio, today=today)
        opinion = advisor.review(brief)
        advisor_log = opinion.as_log()
        if opinion.veto:
            journal.log_decision(symbol=symbol, action="declined",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(sized),
                                 risk={**risk_log, "advisor": advisor_log},
                                 command=command, notes=cand.notes,
                                 reason=f"advisor veto: {opinion.reason}")
            continue

        logger.info("%s → %s | mid %.2f natural %.2f limit %.2f | risk %.0f",
                    symbol, sized.describe(), abs(sized.net_mid),
                    sized.natural_price, limit, risk_at_limit)

        if dry_run:
            journal.log_decision(symbol=symbol, action="declined",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(sized),
                                 risk={**risk_log, "advisor": advisor_log},
                                 command=command, notes=cand.notes,
                                 reason="dry run — not submitted")
            continue

        try:
            order = cli.submit_mleg(legs, qty=qty, limit_price=limit)
        except cli.AlpacaCLIError as exc:
            journal.log_decision(symbol=symbol, action="error",
                                 regime=cand.regime.as_log(),
                                 spread=journal.spread_snapshot(sized),
                                 risk={**risk_log, "advisor": advisor_log},
                                 command=command,
                                 reason=f"submit rejected: {exc}")
            continue

        journal.log_decision(symbol=symbol, action="opened",
                             regime=cand.regime.as_log(),
                             spread=journal.spread_snapshot(sized),
                             risk={**risk_log, "advisor": advisor_log},
                             order=order, command=command, notes=cand.notes,
                             reason=cand.rationale)
        opened += 1
        open_risk += risk_at_limit
        pending.append(execution.Pending(
            symbol=symbol, spread=sized, order_id=str(order.get("id") or ""),
            limit_price=limit, aggression=args.aggression,
            risk_budget=verdict.risk_budget))

    # ── chase ────────────────────────────────────────────────────────────────
    chase_summary = None
    if pending and args.chase_rounds > 0:
        logger.info("chasing %d unfilled package(s)", len(pending))
        execution.chase(pending, rounds=args.chase_rounds,
                        wait_seconds=args.chase_wait)
        chase_summary = execution.summarise(pending)
        for item in pending:
            journal.log_decision(symbol=item.symbol, action="execution",
                                 regime={}, reason=f"chase: {item.status}",
                                 order=item.as_log())
        logger.info("chase result: %s", chase_summary)

    advisor_warning = advisor.sanity_check(len(candidates))
    if advisor_warning:
        logger.warning(advisor_warning)

    account_after = cli.account()
    status = ("dry_run" if dry_run
              else "traded" if opened else "no_trades")
    journal.log_run({
        "date": str(today),
        "status": status,
        "mode": mode,
        "equity_before": equity,
        "equity_after": float(account_after.get("equity") or 0),
        "cash": float(account_after.get("cash") or 0),
        "high_water_mark": hwm,
        "open_legs": len(broker_legs),
        "open_spreads": len(open_spreads),
        "open_risk": round(open_risk, 2),
        "unexplained_positions": [u.as_log() for u in unexplained],
        "candidates": len(candidates),
        "opened": opened,
        "closed": len(closed),
        "chase": chase_summary,
        "advisor": {
            "enabled": advisor.enabled,
            "model": advisor.model if advisor.enabled else None,
            "vetoes": sum(1 for v in advisor.verdicts if v.veto),
            "warning": advisor_warning,
        },
        "universe": universe,
    })
    logger.info("done — %d opened, %d closed", opened, len(closed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
