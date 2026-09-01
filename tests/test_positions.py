"""Reconciling broker legs back into spreads.

Two live bugs are pinned here, and both were invisible from the broker's own
view of the account:

* open risk summed `abs(cost_basis)` across legs, which for a credit spread is
  not the worst case and is not even the right order of magnitude;
* the profit target was applied per leg, so a credit spread's short leg could
  trigger a close that stranded the long one.
"""
from datetime import date

import pytest

from agent import positions as pos_mod

TODAY = date(2026, 9, 2)
EXP = "2026-09-18"


def leg(symbol: str, qty: int, *, cost_basis: float = 0.0,
        unrealized_pl: float = 0.0, current_price: float | None = None) -> dict:
    row = {"symbol": symbol, "qty": str(qty), "cost_basis": str(cost_basis),
           "unrealized_pl": str(unrealized_pl)}
    if current_price is not None:
        row["current_price"] = str(current_price)
    return row


def opened(underlying: str, kind: str, long_symbol: str, short_symbol: str, *,
           qty: int = 10, max_loss: float = 1000.0, max_gain: float = 500.0,
           timestamp: str = "2026-09-01T14:00:00+00:00") -> dict:
    return {
        "timestamp": timestamp, "symbol": underlying, "action": "opened",
        "spread": {"kind": kind, "underlying": underlying, "expiration": EXP,
                   "long_leg": long_symbol, "short_leg": short_symbol,
                   "qty": qty, "max_loss": max_loss, "max_gain": max_gain},
    }


CREDIT_LONG = "AAPL260918P00300000"
CREDIT_SHORT = "AAPL260918P00302500"


class TestOpenRisk:
    def test_a_credit_spread_reports_its_recorded_worst_case(self):
        """Not its cost basis.

        The position below collected $382 of credit against $1,867 of width.
        Summing |cost_basis| over the legs answers a question nobody asked and
        returns a number roughly five times too large; the gate needs the
        width minus the credit, which is what the journal recorded at entry.
        """
        legs = [leg(CREDIT_LONG, 9, cost_basis=4500),
                leg(CREDIT_SHORT, -9, cost_basis=-4882)]
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                            qty=9, max_loss=1867.5, max_gain=382.5)]
        spreads, unexplained = pos_mod.reconcile(legs, decisions)

        assert pos_mod.open_risk(spreads) == pytest.approx(1867.5)
        assert sum(abs(float(p["cost_basis"])) for p in legs) == pytest.approx(9382)
        assert unexplained == []

    def test_a_partial_fill_scales_the_recorded_risk(self):
        """Nine contracts were sized; three are on. The risk is three ninths.

        The journal is authoritative about structure, the broker about size.
        Trusting the journal's quantity here would report risk that is not on.
        """
        legs = [leg(CREDIT_LONG, 3), leg(CREDIT_SHORT, -3)]
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                            qty=9, max_loss=1800.0, max_gain=900.0)]
        spreads, _ = pos_mod.reconcile(legs, decisions)
        assert spreads[0].qty == 3
        assert spreads[0].max_loss == pytest.approx(600.0)
        assert spreads[0].max_gain == pytest.approx(300.0)

    def test_a_lone_leg_is_reported_as_partial_not_averaged_away(self):
        legs = [leg(CREDIT_SHORT, -9, cost_basis=-4882)]
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                            qty=9, max_loss=1867.5)]
        spreads, unexplained = pos_mod.reconcile(legs, decisions)
        assert spreads[0].state == pos_mod.PARTIAL
        assert unexplained == spreads

    def test_an_orphan_short_leg_is_charged_its_full_notional(self):
        """A naked short is undefined risk and must dominate the gate.

        The agent never opens one, so a leg it cannot explain is either a bug
        or a manual action. Charging it the strike notional stops all new
        trading until a human has looked, which is the intended outcome.
        """
        legs = [leg("AAPL260918P00302500", -2, cost_basis=-900)]
        spreads, unexplained = pos_mod.reconcile(legs, [])
        assert spreads[0].state == pos_mod.ORPHAN
        assert spreads[0].max_loss == pytest.approx(302.5 * 100 * 2)
        assert unexplained == spreads

    def test_an_orphan_long_leg_risks_only_what_it_cost(self):
        legs = [leg("AAPL260918P00302500", 2, cost_basis=900)]
        spreads, _ = pos_mod.reconcile(legs, [])
        assert spreads[0].max_loss == pytest.approx(900)

    def test_equity_positions_are_ignored(self):
        spreads, unexplained = pos_mod.reconcile([leg("AAPL", 100)], [])
        assert spreads == [] and unexplained == []

    def test_a_closed_spread_leaves_nothing_behind(self):
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT)]
        spreads, _ = pos_mod.reconcile([], decisions)
        assert spreads == []

    def test_the_newest_record_wins_when_legs_are_reused(self):
        """Re-opening the same strikes must not double-count the risk."""
        legs = [leg(CREDIT_LONG, 5), leg(CREDIT_SHORT, -5)]
        decisions = [
            opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                   qty=5, max_loss=999.0, timestamp="2026-08-20T14:00:00+00:00"),
            opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                   qty=5, max_loss=1500.0, timestamp="2026-09-01T14:00:00+00:00"),
        ]
        spreads, _ = pos_mod.reconcile(legs, decisions)
        assert len(spreads) == 1
        assert spreads[0].max_loss == pytest.approx(1500.0)


class TestProfitFraction:
    """The 60% target, measured on the package."""

    def _spread(self, long_pl: float, short_pl: float, max_gain: float = 500.0):
        legs = [leg(CREDIT_LONG, 9, unrealized_pl=long_pl),
                leg(CREDIT_SHORT, -9, unrealized_pl=short_pl)]
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                            qty=9, max_loss=1800.0, max_gain=max_gain)]
        return pos_mod.reconcile(legs, decisions)[0][0]

    def test_package_pl_is_the_sum_of_the_legs(self):
        assert self._spread(-120, 400).unrealized_pl == pytest.approx(280)

    def test_a_short_leg_far_in_profit_does_not_reach_the_package_target(self):
        """The exact shape that used to trigger a leg-only close.

        The short put has decayed almost entirely — on its own it is up 80% —
        while the long put has lost value too, so the package has earned 56% of
        its maximum, which is below the 60% target. The old per-leg rule would
        have closed the short leg here and left the long put stranded.
        """
        spread = self._spread(-120, 400, max_gain=500.0)
        assert spread.profit_fraction == pytest.approx(0.56)
        assert spread.profit_fraction < 0.60

    def test_no_recorded_max_gain_means_no_fraction_rather_than_zero(self):
        # A zero would read as "0% of target" and quietly never close.
        assert self._spread(10, 10, max_gain=0.0).profit_fraction is None

    def test_a_losing_package_reports_a_negative_fraction(self):
        assert self._spread(-300, -100).profit_fraction < 0


class TestClosingLegs:
    def test_closing_reverses_every_side_and_intent(self):
        legs = [leg(CREDIT_LONG, 9), leg(CREDIT_SHORT, -9)]
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT,
                            qty=9)]
        spread = pos_mod.reconcile(legs, decisions)[0][0]
        payload = {row["symbol"]: row for row in spread.closing_legs()}

        assert payload[CREDIT_LONG]["side"] == "sell"
        assert payload[CREDIT_LONG]["position_intent"] == "sell_to_close"
        assert payload[CREDIT_SHORT]["side"] == "buy"
        assert payload[CREDIT_SHORT]["position_intent"] == "buy_to_close"

    def test_dte_comes_from_the_occ_symbol_not_the_journal(self):
        legs = [leg(CREDIT_LONG, 9), leg(CREDIT_SHORT, -9)]
        decisions = [opened("AAPL", "bull_put_credit", CREDIT_LONG, CREDIT_SHORT)]
        spread = pos_mod.reconcile(legs, decisions)[0][0]
        assert spread.dte(TODAY) == 16
