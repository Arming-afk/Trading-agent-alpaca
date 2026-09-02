"""Portfolio risk gates.

Four independent checks stand between a decision and an order. Each is a pure
function of state that is passed in, so each is directly testable and none can
be quietly skipped by a code path that forgot to call it — `evaluate()` runs
all of them and an order is submitted only on an unanimous pass.

Two design rules worth stating because they are easy to erode later:

1. **The defaults are a-priori.** They were chosen before the competition
   started and are not tuned against its results. Five trading days cannot
   distinguish a good threshold from a lucky one, and tuning a risk limit on
   the record it is being judged by is circular.

2. **Missing data blocks new risk.** These gates differ from a monitoring
   system, where a missing datum should mean "do not intervene". Here the gate
   is the only thing sizing the position, so an unknown equity means no trade
   rather than an unsized one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent import config
from agent.spreads import Vertical


@dataclass
class GateResult:
    """Outcome of one gate. `detail` is written to the decision log so a
    blocked trade can be explained after the fact."""
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RiskVerdict:
    allowed: bool
    gates: list[GateResult] = field(default_factory=list)
    #: Dollars of max-loss this trade may consume; 0 when blocked.
    risk_budget: float = 0.0

    @property
    def blocked_by(self) -> list[str]:
        return [g.name for g in self.gates if not g.passed]

    def reason(self) -> str:
        failures = [f"{g.name}: {g.detail}" for g in self.gates if not g.passed]
        return "; ".join(failures) if failures else "all gates passed"


def drawdown_gate(equity: float | None, high_water_mark: float | None,
                  max_drawdown_pct: float | None = None) -> GateResult:
    """Halt new positions when equity has fallen this far below its peak.

    Existing spreads are left alone: they are defined-risk and already paid
    for, and closing them into a drawdown realises the loss the breaker exists
    to limit.
    """
    limit = config.MAX_DRAWDOWN_PCT if max_drawdown_pct is None else max_drawdown_pct
    if limit <= 0:
        return GateResult("drawdown", True, "disabled")
    if equity is None or high_water_mark is None or high_water_mark <= 0:
        return GateResult("drawdown", False, "equity or high-water mark unavailable")

    dd = (high_water_mark - equity) / high_water_mark * 100
    if dd >= limit:
        return GateResult("drawdown", False, f"drawdown {dd:.2f}% >= {limit:.2f}%")
    return GateResult("drawdown", True, f"drawdown {max(dd, 0.0):.2f}%")


def portfolio_risk_gate(open_risk: float, equity: float | None,
                        max_portfolio_risk_pct: float | None = None) -> GateResult:
    """Cap the sum of max-loss across all open spreads.

    This is the number that a per-trade cap alone does not control: twenty
    trades each risking 2% is a 40% portfolio bet, not a conservative one.
    """
    limit = (config.MAX_PORTFOLIO_RISK_PCT if max_portfolio_risk_pct is None
             else max_portfolio_risk_pct)
    if limit <= 0:
        return GateResult("portfolio_risk", True, "disabled")
    if equity is None or equity <= 0:
        return GateResult("portfolio_risk", False, "equity unavailable")

    used_pct = open_risk / equity * 100
    if used_pct >= limit:
        return GateResult("portfolio_risk", False,
                          f"open risk {used_pct:.2f}% >= {limit:.2f}%")
    return GateResult("portfolio_risk", True,
                      f"open risk {used_pct:.2f}% of {limit:.2f}%")


def underlying_risk_gate(underlying: str | None, underlying_risk: float,
                         equity: float | None,
                         max_underlying_risk_pct: float | None = None) -> GateResult:
    """Cap the sum of max-loss across every open spread on one underlying.

    The gap this closes is between the two caps that already existed. The
    per-trade budget looks at one spread and the portfolio cap looks at all of
    them; neither looks at a name. So the agent could read AAPL as rich on
    consecutive days, open a 2%-of-equity spread each time — every one of them
    approved on its own terms — and end up with 5.2% of the account riding on
    one strike pair and one expiry. Two entries at the same strikes are not two
    positions; the broker nets them into one, and so does a gap down.

    This gate blocks *new* risk in a name that is already full. It never closes
    what is open, for the same reason the drawdown breaker does not: those
    spreads are defined-risk and already paid for.
    """
    limit = (config.MAX_UNDERLYING_RISK_PCT if max_underlying_risk_pct is None
             else max_underlying_risk_pct)
    if limit <= 0:
        return GateResult("underlying_risk", True, "disabled")
    if not underlying:
        # Callers that do not name the underlying are not exempt from the rule;
        # they simply cannot be evaluated against it, and saying so in the log
        # is better than a silent pass that reads like one.
        return GateResult("underlying_risk", True, "no underlying supplied")
    if equity is None or equity <= 0:
        return GateResult("underlying_risk", False, "equity unavailable")

    used_pct = underlying_risk / equity * 100
    if used_pct >= limit:
        return GateResult("underlying_risk", False,
                          f"{underlying} already carries {used_pct:.2f}% "
                          f">= {limit:.2f}%")
    return GateResult("underlying_risk", True,
                      f"{underlying} at {used_pct:.2f}% of {limit:.2f}%")


def daily_trade_gate(trades_today: int,
                     max_new_trades: int | None = None) -> GateResult:
    """Cap new positions per day. Guards against a bug or a bad news day
    turning into twenty correlated trades in one session."""
    limit = (config.MAX_NEW_TRADES_PER_DAY if max_new_trades is None
             else max_new_trades)
    if limit <= 0:
        return GateResult("daily_trades", True, "disabled")
    if trades_today >= limit:
        return GateResult("daily_trades", False, f"{trades_today} opened today, cap {limit}")
    return GateResult("daily_trades", True, f"{trades_today}/{limit} today")


def trade_risk_budget(equity: float | None,
                      max_trade_risk_pct: float | None = None) -> float:
    """Dollars of max-loss a single new spread may carry."""
    limit = (config.MAX_TRADE_RISK_PCT if max_trade_risk_pct is None
             else max_trade_risk_pct)
    if equity is None or equity <= 0 or limit <= 0:
        return 0.0
    return equity * limit / 100


def open_risk_from_positions(spreads: list[Vertical]) -> float:
    """Total max-loss currently at risk across open spreads."""
    return sum(s.max_loss for s in spreads)


def evaluate(*, equity: float | None, high_water_mark: float | None,
             open_risk: float, trades_today: int,
             underlying: str | None = None, underlying_risk: float = 0.0,
             overrides: dict | None = None) -> RiskVerdict:
    """Run every gate. An order is allowed only if all of them pass."""
    o = overrides or {}
    gates = [
        drawdown_gate(equity, high_water_mark, o.get("max_drawdown_pct")),
        portfolio_risk_gate(open_risk, equity, o.get("max_portfolio_risk_pct")),
        underlying_risk_gate(underlying, underlying_risk, equity,
                             o.get("max_underlying_risk_pct")),
        daily_trade_gate(trades_today, o.get("max_new_trades")),
    ]
    allowed = all(g.passed for g in gates)
    budget = trade_risk_budget(equity, o.get("max_trade_risk_pct")) if allowed else 0.0

    # Never let one trade exceed what is left under a cap it has to share.
    # Both headrooms are applied, for the same reason the portfolio one always
    # was: a cap that only blocks the trade that crosses it is breachable one
    # trade at a time.
    if allowed and equity and equity > 0:
        pf_limit = (config.MAX_PORTFOLIO_RISK_PCT
                    if o.get("max_portfolio_risk_pct") is None
                    else o["max_portfolio_risk_pct"])
        if pf_limit > 0:
            headroom = max(equity * pf_limit / 100 - open_risk, 0.0)
            budget = min(budget, headroom)

        ul_limit = (config.MAX_UNDERLYING_RISK_PCT
                    if o.get("max_underlying_risk_pct") is None
                    else o["max_underlying_risk_pct"])
        if ul_limit > 0 and underlying:
            headroom = max(equity * ul_limit / 100 - underlying_risk, 0.0)
            budget = min(budget, headroom)

    return RiskVerdict(allowed=allowed and budget > 0, gates=gates, risk_budget=budget)


def high_water_mark(equity_history: list[float], current: float | None = None) -> float | None:
    """Peak equity seen so far, including the current reading."""
    values = [e for e in equity_history if isinstance(e, (int, float)) and e > 0]
    if current and current > 0:
        values.append(float(current))
    return max(values) if values else None
