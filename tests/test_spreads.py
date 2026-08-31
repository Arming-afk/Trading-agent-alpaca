"""The defined-risk arithmetic. Every number here is hand-computed, because
max_loss is what sizes every position — a silent error here mis-sizes the
whole book rather than one trade."""
import pytest

from agent.spreads import (BEAR_CALL_CREDIT, BEAR_PUT_DEBIT, BULL_CALL_DEBIT,
                           BULL_PUT_CREDIT, MULTIPLIER, Vertical, build,
                           size_for_risk)
from tests.conftest import contract


def call(strike, mid):
    """A call quoted 0.10 wide around `mid`."""
    return contract(strike, "call", bid=mid - 0.05, ask=mid + 0.05)


def put(strike, mid):
    return contract(strike, "put", bid=mid - 0.05, ask=mid + 0.05)


class TestBullCallDebit:
    """Long 100c @ 5.00, short 105c @ 3.00 → 2.00 debit on a 5-wide spread."""

    @pytest.fixture
    def spread(self):
        return build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])

    def test_long_leg_is_the_lower_strike(self, spread):
        assert spread.long_leg.strike == 100
        assert spread.short_leg.strike == 105

    def test_is_a_debit(self, spread):
        assert spread.is_debit
        assert spread.net_mid == pytest.approx(2.00)

    def test_max_loss_is_the_debit_paid(self, spread):
        assert spread.max_loss == pytest.approx(200.0)

    def test_max_gain_is_width_minus_debit(self, spread):
        assert spread.max_gain == pytest.approx(300.0)

    def test_breakeven_is_long_strike_plus_debit(self, spread):
        assert spread.breakeven == pytest.approx(102.0)

    def test_reward_risk(self, spread):
        assert spread.reward_risk == pytest.approx(1.5)

    def test_quantity_scales_both_sides_of_the_risk(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)], qty=3)
        assert s.max_loss == pytest.approx(600.0)
        assert s.max_gain == pytest.approx(900.0)


class TestBullPutCredit:
    """Short 100p @ 3.00, long 95p @ 1.50 → 1.50 credit on a 5-wide spread."""

    @pytest.fixture
    def spread(self):
        return build(BULL_PUT_CREDIT, [put(95, 1.50), put(100, 3.00)])

    def test_long_leg_is_the_lower_strike(self, spread):
        assert spread.long_leg.strike == 95
        assert spread.short_leg.strike == 100

    def test_is_a_credit(self, spread):
        assert not spread.is_debit
        assert spread.net_mid == pytest.approx(-1.50)

    def test_max_gain_is_the_credit_collected(self, spread):
        assert spread.max_gain == pytest.approx(150.0)

    def test_max_loss_is_width_minus_credit(self, spread):
        assert spread.max_loss == pytest.approx(350.0)

    def test_breakeven_is_short_strike_minus_credit(self, spread):
        assert spread.breakeven == pytest.approx(98.5)


class TestBearPutDebit:
    """Long 105p @ 4.00, short 100p @ 2.00 → 2.00 debit."""

    @pytest.fixture
    def spread(self):
        return build(BEAR_PUT_DEBIT, [put(100, 2.00), put(105, 4.00)])

    def test_long_leg_is_the_higher_strike(self, spread):
        assert spread.long_leg.strike == 105

    def test_max_loss_and_gain(self, spread):
        assert spread.max_loss == pytest.approx(200.0)
        assert spread.max_gain == pytest.approx(300.0)

    def test_breakeven_is_long_strike_minus_debit(self, spread):
        assert spread.breakeven == pytest.approx(103.0)


class TestBearCallCredit:
    """Short 100c @ 3.00, long 105c @ 1.50 → 1.50 credit."""

    @pytest.fixture
    def spread(self):
        return build(BEAR_CALL_CREDIT, [call(100, 3.00), call(105, 1.50)])

    def test_long_leg_is_the_higher_strike(self, spread):
        assert spread.long_leg.strike == 105

    def test_max_loss_and_gain(self, spread):
        assert spread.max_loss == pytest.approx(350.0)
        assert spread.max_gain == pytest.approx(150.0)

    def test_breakeven_is_short_strike_plus_credit(self, spread):
        assert spread.breakeven == pytest.approx(101.5)


class TestInvariants:
    def test_max_loss_plus_max_gain_equals_the_width_for_any_vertical(self):
        """A vertical's outcomes span exactly the strike width — this holds for
        all four kinds and catches a sign error in any single formula."""
        cases = [
            build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)]),
            build(BULL_PUT_CREDIT, [put(95, 1.50), put(100, 3.00)]),
            build(BEAR_PUT_DEBIT, [put(100, 2.00), put(105, 4.00)]),
            build(BEAR_CALL_CREDIT, [call(100, 3.00), call(105, 1.50)]),
        ]
        for s in cases:
            assert s.max_loss + s.max_gain == pytest.approx(s.width * MULTIPLIER), s.kind

    def test_breakeven_always_falls_between_the_strikes(self):
        cases = [
            build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)]),
            build(BULL_PUT_CREDIT, [put(95, 1.50), put(100, 3.00)]),
            build(BEAR_PUT_DEBIT, [put(100, 2.00), put(105, 4.00)]),
            build(BEAR_CALL_CREDIT, [call(100, 3.00), call(105, 1.50)]),
        ]
        for s in cases:
            lo = min(s.long_leg.strike, s.short_leg.strike)
            hi = max(s.long_leg.strike, s.short_leg.strike)
            assert lo <= s.breakeven <= hi, s.kind

    def test_worst_spread_pct_reports_the_worse_leg(self):
        tight = contract(100, "call", bid=4.95, ask=5.05)
        wide = contract(105, "call", bid=2.00, ask=4.00)
        s = build(BULL_CALL_DEBIT, [tight, wide])
        assert s.worst_spread_pct == pytest.approx(wide.spread_pct)


class TestConstruction:
    def test_rejects_mismatched_option_types(self):
        with pytest.raises(ValueError, match="same option type"):
            Vertical(BULL_CALL_DEBIT, call(100, 5.00), put(105, 3.00))

    def test_rejects_identical_strikes(self):
        with pytest.raises(ValueError, match="two different strikes"):
            build(BULL_CALL_DEBIT, [call(100, 5.00), call(100, 3.00)])

    def test_rejects_the_wrong_number_of_legs(self):
        with pytest.raises(ValueError, match="exactly 2"):
            build(BULL_CALL_DEBIT, [call(100, 5.00)])

    def test_rejects_an_unknown_kind(self):
        with pytest.raises(ValueError, match="unknown spread kind"):
            build("iron_condor", [call(100, 5.00), call(105, 3.00)])


class TestLegsPayload:
    @pytest.fixture
    def spread(self):
        return build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])

    def test_opening_legs_buy_the_long_and_sell_the_short(self, spread):
        legs = spread.legs_payload()
        assert [(l["side"], l["position_intent"]) for l in legs] == [
            ("buy", "buy_to_open"), ("sell", "sell_to_open")]

    def test_closing_legs_reverse_both_sides(self, spread):
        legs = spread.legs_payload(closing=True)
        assert [(l["side"], l["position_intent"]) for l in legs] == [
            ("sell", "sell_to_close"), ("buy", "buy_to_close")]

    def test_legs_carry_the_occ_symbols(self, spread):
        legs = spread.legs_payload()
        assert legs[0]["symbol"] == spread.long_leg.symbol
        assert legs[1]["symbol"] == spread.short_leg.symbol


class TestLimitPrice:
    """Legs are quoted 0.10 wide by the helpers, so on a two-leg spread the
    mid-to-natural distance is 0.10: mid 2.00, natural 2.10 for the debit."""

    def test_natural_price_crosses_both_markets(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])
        # buy the 100c at 5.05, sell the 105c at 2.95 → 2.10
        assert s.natural_price == pytest.approx(2.10)

    def test_a_credit_natural_is_the_worse_credit(self):
        s = build(BULL_PUT_CREDIT, [put(95, 1.50), put(100, 3.00)])
        # sell the 100p at 2.95, buy the 95p at 1.55 → 1.40 vs a 1.50 mid
        assert s.natural_price == pytest.approx(1.40)

    def test_zero_aggression_prices_at_mid(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])
        assert s.limit_price(aggression=0) == pytest.approx(2.00)

    def test_full_aggression_prices_at_natural(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])
        assert s.limit_price(aggression=1) == pytest.approx(2.10)

    def test_half_aggression_splits_the_distance(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])
        assert s.limit_price(aggression=0.5) == pytest.approx(2.05)

    def test_a_debit_limit_sits_between_mid_and_natural(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])
        assert abs(s.net_mid) <= s.limit_price(0.5) <= s.natural_price

    def test_a_credit_limit_sits_between_natural_and_mid(self):
        s = build(BULL_PUT_CREDIT, [put(95, 1.50), put(100, 3.00)])
        assert s.natural_price <= s.limit_price(0.5) <= abs(s.net_mid)

    def test_the_concession_scales_with_the_width_of_the_market(self):
        """The reason this replaced a percentage of mid. Two spreads with the
        same mid but different market widths must concede different amounts."""
        tight = build(BULL_CALL_DEBIT,
                      [contract(100, "call", bid=4.99, ask=5.01),
                       contract(105, "call", bid=2.99, ask=3.01)])
        wide = build(BULL_CALL_DEBIT,
                     [contract(100, "call", bid=4.60, ask=5.40),
                      contract(105, "call", bid=2.60, ask=3.40)])
        assert abs(tight.net_mid) == pytest.approx(abs(wide.net_mid))
        assert tight.limit_price(0.5) < wide.limit_price(0.5)

    def test_a_debit_rounds_up_so_it_is_never_less_marketable(self):
        s = build(BULL_CALL_DEBIT,
                  [contract(100, "call", bid=4.99, ask=5.02),
                   contract(105, "call", bid=2.99, ask=3.02)])
        # mid 2.005, natural 2.03 → half is 2.0175, must not round down to 2.01
        assert s.limit_price(0.5) == pytest.approx(2.02)

    def test_a_credit_rounds_down_so_it_is_never_less_marketable(self):
        s = build(BULL_PUT_CREDIT,
                  [contract(95, "put", bid=1.48, ask=1.52),
                   contract(100, "put", bid=2.98, ask=3.02)])
        # mid 1.50, natural 1.46 → half is 1.48; rounding must not lift it
        assert s.limit_price(0.5) <= 1.48

    def test_the_limit_is_always_a_positive_net_price(self):
        s = build(BULL_PUT_CREDIT, [put(95, 1.50), put(100, 3.00)])
        assert s.limit_price() > 0

    def test_a_credit_worth_nothing_at_natural_still_prices_above_zero(self):
        """Legs so wide that crossing both would pay nothing — the order must
        still carry a legal price rather than 0.00."""
        s = build(BULL_PUT_CREDIT,
                  [contract(95, "put", bid=0.10, ask=3.00),
                   contract(100, "put", bid=0.20, ask=3.10)])
        assert s.limit_price(1.0) >= 0.01

    def test_aggression_is_clamped_to_the_unit_interval(self):
        s = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])
        assert s.limit_price(5.0) == pytest.approx(s.limit_price(1.0))
        assert s.limit_price(-2.0) == pytest.approx(s.limit_price(0.0))


class TestSizing:
    @pytest.fixture
    def spread(self):
        # one contract risks $200
        return build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])

    def test_fits_as_many_whole_contracts_as_the_budget_allows(self, spread):
        assert size_for_risk(spread, 500) == 2      # 2x200=400 fits, 3x would not

    def test_returns_zero_when_one_contract_is_too_expensive(self, spread):
        assert size_for_risk(spread, 150) == 0

    def test_an_exact_multiple_is_not_rounded_down(self, spread):
        assert size_for_risk(spread, 600) == 3

    def test_a_zero_budget_buys_nothing(self, spread):
        assert size_for_risk(spread, 0) == 0

    def test_sizing_ignores_the_qty_already_on_the_spread(self, spread):
        """Budget is measured against one contract, so passing an already-sized
        spread must not compound the quantity."""
        sized = Vertical(spread.kind, spread.long_leg, spread.short_leg, qty=5)
        assert size_for_risk(sized, 500) == 2
