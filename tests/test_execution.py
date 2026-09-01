"""The fill chase, and the sizing rule it has to respect.

On 2026-08-31 the agent submitted three packages and one traded. The other two
sat at the midpoint until the day order expired, and the decision log recorded
all three as trades. These tests pin both halves of the fix: that an unfilled
order is walked toward the market, and that walking it never spends risk budget
the gate did not grant.
"""
import pytest

from agent import cli, execution, spreads
from agent.positions import OpenSpread
from tests.conftest import contract


def call(strike, bid, ask):
    return contract(strike, "call", bid=bid, ask=ask)


@pytest.fixture
def debit():
    """A 2.5-wide call debit spread: mid 1.25, natural 1.40."""
    return spreads.build(spreads.BULL_CALL_DEBIT,
                         [call(220.0, 8.00, 8.20), call(222.5, 6.80, 6.90)],
                         qty=1)


def pending(spread, *, qty=16, limit=1.31, aggression=0.5, budget=2000.0):
    sized = spreads.Vertical(spread.kind, spread.long_leg, spread.short_leg, qty=qty)
    return execution.Pending(symbol=sized.long_leg.underlying, spread=sized,
                             order_id="order-1", limit_price=limit,
                             aggression=aggression, risk_budget=budget)


class FakeBroker:
    """Stands in for the CLI. Records every call so the tests can assert on
    what was actually sent, not on what the code intended to send."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.submissions = []
        self.cancels = []
        self._next_id = 1

    def get_order(self, order_id):
        status = self.statuses.pop(0) if self.statuses else ("new", 0)
        return {"status": status[0], "filled_qty": str(status[1])}

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return {}

    def submit_mleg(self, legs, qty, *, limit_price, **kwargs):
        self._next_id += 1
        self.submissions.append({"qty": qty, "limit": limit_price, "legs": legs})
        return {"id": f"order-{self._next_id}"}


@pytest.fixture
def broker(monkeypatch):
    def install(statuses):
        fake = FakeBroker(statuses)
        monkeypatch.setattr(cli, "get_order", fake.get_order)
        monkeypatch.setattr(cli, "cancel_order", fake.cancel_order)
        monkeypatch.setattr(cli, "submit_mleg", fake.submit_mleg)
        monkeypatch.setattr(execution.cli, "get_order", fake.get_order)
        monkeypatch.setattr(execution.cli, "cancel_order", fake.cancel_order)
        monkeypatch.setattr(execution.cli, "submit_mleg", fake.submit_mleg)
        return fake
    return install


NO_WAIT = lambda _seconds: None      # noqa: E731 — the point is that it is inert


class TestChase:
    def test_a_filled_order_is_left_alone(self, debit, broker):
        fake = broker([("filled", 16)])
        item = pending(debit)
        execution.chase([item], rounds=3, sleep=NO_WAIT)

        assert item.status == execution.FILLED
        assert fake.submissions == []
        assert fake.cancels == []

    def test_an_unfilled_order_is_requoted_closer_to_the_market(self, debit, broker):
        fake = broker([("new", 0), ("filled", 15)])
        item = pending(debit)
        execution.chase([item], rounds=3, step=0.25, sleep=NO_WAIT)

        assert len(fake.submissions) == 1
        assert item.status == execution.FILLED
        # Mid 1.25, natural 1.40. Aggression 0.5 → 1.33 (rounded up); 0.75 →
        # 1.3625 → 1.37. The concession scales with the width of the market,
        # not with the price.
        assert fake.submissions[0]["limit"] == pytest.approx(1.37)
        assert item.aggression == pytest.approx(0.75)

    def test_every_requote_is_resized_to_the_budget(self, debit, broker):
        """The rule the whole module exists to protect.

        Sixteen contracts at 1.31 is $2,096 against a $2,000 budget — the
        breach that shipped. At the improved 1.37 the position has to shrink to
        fourteen, because 15 x 1.37 x 100 is $2,055 and still over.
        """
        fake = broker([("new", 0), ("filled", 14)])
        item = pending(debit, qty=16, limit=1.31, budget=2000.0)
        execution.chase([item], rounds=3, step=0.25, sleep=NO_WAIT)

        submitted = fake.submissions[0]
        assert submitted["qty"] == 14
        assert submitted["qty"] * submitted["limit"] * 100 <= 2000.0
        assert item.spread.max_loss_at(item.limit_price) <= 2000.0

    def test_the_old_order_is_cancelled_before_the_new_one_is_sent(self, debit, broker):
        fake = broker([("new", 0), ("filled", 14)])
        item = pending(debit)
        execution.chase([item], rounds=3, sleep=NO_WAIT)
        assert fake.cancels == ["order-1"]

    def test_a_chase_that_cannot_fit_the_budget_is_abandoned_not_forced(
            self, debit, broker):
        fake = broker([("new", 0)])
        # A budget that one contract cannot fit at any improved price.
        item = pending(debit, qty=1, limit=1.31, budget=100.0)
        execution.chase([item], rounds=3, sleep=NO_WAIT)

        assert item.status == execution.ABANDONED
        assert fake.submissions == []
        assert "no size fits the budget" in item.attempts[-1]["why"]

    def test_crossing_both_markets_is_the_last_price_offered(self, debit, broker):
        fake = broker([("new", 0)])
        item = pending(debit, aggression=1.0)
        execution.chase([item], rounds=3, sleep=NO_WAIT)

        assert item.status == execution.ABANDONED
        assert fake.submissions == []
        assert item.attempts[-1]["why"] == "already at the marketable price"

    def test_a_partial_fill_is_reported_and_not_improved_on(self, debit, broker):
        """The position is already on and is not the shape that was sized."""
        fake = broker([("partially_filled", 7)])
        item = pending(debit, qty=16)
        execution.chase([item], rounds=3, sleep=NO_WAIT)

        assert item.status == execution.PARTIAL
        assert item.filled_qty == 7
        assert fake.submissions == []

    def test_a_dead_order_stops_the_chase(self, debit, broker):
        broker([("canceled", 0)])
        item = pending(debit)
        execution.chase([item], rounds=3, sleep=NO_WAIT)
        assert item.status == execution.ABANDONED

    def test_an_order_still_working_at_the_end_is_left_resting(self, debit, broker):
        """A day order that fills after the run ends is still a fill."""
        fake = broker([("new", 0)])
        item = pending(debit)
        execution.chase([item], rounds=1, sleep=NO_WAIT)

        assert item.status == execution.WORKING
        assert item.attempts[-1]["action"] == "left_resting"
        assert item.order_id not in fake.cancels

    def test_an_unreadable_order_is_not_treated_as_filled(self, debit, monkeypatch):
        monkeypatch.setattr(execution.cli, "get_order",
                            lambda _id: (_ for _ in ()).throw(
                                cli.AlpacaCLIError("boom")))
        assert execution.poll("order-1") == ("unknown", 0)

    def test_summarise_counts_every_terminal_state(self, debit, broker):
        broker([("filled", 16)])
        one = pending(debit)
        execution.chase([one], rounds=1, sleep=NO_WAIT)
        assert execution.summarise([one])["filled"] == 1


class TestClosePrice:
    def _spread(self, long_price, short_price, qty=10):
        return OpenSpread(
            underlying="AAPL", kind="bull_put_credit", expiration=None,
            long_symbol="L", short_symbol="S", qty=qty,
            max_loss=1800.0, max_gain=500.0,
            legs=[{"symbol": "L", "qty": str(qty), "current_price": str(long_price)},
                  {"symbol": "S", "qty": str(-qty), "current_price": str(short_price)}])

    def test_buying_back_a_credit_spread_concedes_upward(self):
        """Closing costs 0.45 net; we offer more than that to actually get out."""
        price = execution.close_price(self._spread(1.20, 1.65))
        assert price > 0.45
        assert price == pytest.approx(0.50)

    def test_selling_out_of_a_debit_spread_concedes_downward(self):
        # Long leg worth 1.65, short leg worth 1.20 → we receive 0.45 net.
        price = execution.close_price(self._spread(1.65, 1.20))
        assert price < 0.45

    def test_the_concession_never_falls_below_the_floor(self):
        """A percentage concession on a cheap package is not an exit.

        Four percent of a $0.10 package is less than a cent, which on legs
        quoted a nickel wide will never trade — and the position this is trying
        to close is one the agent has already decided it does not want.
        """
        price = execution.close_price(self._spread(0.05, 0.15))
        assert price >= 0.10 + execution.MIN_CLOSE_CONCESSION - 1e-9

    def test_a_missing_mark_refuses_to_guess(self):
        spread = self._spread(1.20, 1.65)
        spread.legs[0].pop("current_price")
        assert execution.close_price(spread) is None


class TestCloseSpread:
    def test_a_one_legged_position_is_not_closed_as_a_package(self):
        spread = OpenSpread("AAPL", "orphan", None, "L", None, 2, 100.0, 0.0,
                            legs=[{"symbol": "L", "qty": "2", "current_price": "1"}])
        result = execution.close_spread(spread, reason="orphan")
        assert result["submitted"] is False
        assert "not a closable package" in result["why"]

    def test_an_unpriceable_position_is_reported_not_market_ordered(self):
        spread = OpenSpread("AAPL", "bull_put_credit", None, "L", "S", 2,
                            100.0, 50.0,
                            legs=[{"symbol": "L", "qty": "2"},
                                  {"symbol": "S", "qty": "-2"}])
        result = execution.close_spread(spread)
        assert result["submitted"] is False
        assert "cannot price the exit" in result["why"]

    def test_a_close_goes_out_as_one_mleg_order(self, broker):
        fake = broker([])
        spread = OpenSpread("AAPL", "bull_put_credit", None, "L", "S", 9,
                            1800.0, 500.0,
                            legs=[{"symbol": "L", "qty": "9", "current_price": "1.20"},
                                  {"symbol": "S", "qty": "-9", "current_price": "1.65"}])
        result = execution.close_spread(spread, reason="profit target")

        assert result["submitted"] is True
        assert len(fake.submissions) == 1
        assert len(fake.submissions[0]["legs"]) == 2
        assert fake.submissions[0]["qty"] == 9
