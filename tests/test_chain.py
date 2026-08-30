from datetime import date

import pytest

from agent.chain import (Contract, build_chain, by_delta, by_strike, from_snapshot,
                         is_tradable, nearest_expiry, parse_occ)
from tests.conftest import EXP, contract


class TestParseOCC:
    def test_parses_a_standard_call(self):
        underlying, expiration, kind, strike = parse_occ("AAPL260918C00190000")
        assert underlying == "AAPL"
        assert expiration == date(2026, 9, 18)
        assert kind == "call"
        assert strike == 190.0

    def test_parses_a_fractional_strike_put(self):
        _, _, kind, strike = parse_occ("SPY260918P00512500")
        assert kind == "put"
        assert strike == 512.5

    def test_rejects_an_equity_symbol(self):
        with pytest.raises(ValueError):
            parse_occ("AAPL")

    def test_roundtrips_the_symbol_the_test_helper_builds(self):
        c = contract(187.5, "put")
        underlying, expiration, kind, strike = parse_occ(c.symbol)
        assert (underlying, expiration, kind, strike) == ("AAPL", EXP, "put", 187.5)


class TestQuoteMath:
    def test_mid_is_the_midpoint(self):
        assert contract(100, bid=1.00, ask=1.50).mid == 1.25

    def test_spread_pct_is_width_over_mid(self):
        # width 0.50 on a mid of 1.25 = 40%
        assert contract(100, bid=1.00, ask=1.50).spread_pct == pytest.approx(40.0)

    def test_a_contract_with_no_bid_has_no_mid_and_infinite_spread(self):
        c = contract(100, bid=0.0, ask=1.50)
        assert c.mid == 0.0
        assert c.spread_pct == float("inf")


class TestTradability:
    TODAY = date(2026, 9, 1)   # 17 DTE from EXP

    def test_accepts_a_liquid_contract(self):
        assert is_tradable(contract(100, bid=1.00, ask=1.05), today=self.TODAY)

    def test_rejects_a_wide_market(self):
        wide = contract(100, bid=1.00, ask=2.00)   # 66% spread
        assert not is_tradable(wide, today=self.TODAY)

    def test_rejects_thin_open_interest(self):
        assert not is_tradable(contract(100, oi=5), today=self.TODAY,
                               min_open_interest=100)

    def test_rejects_a_contract_expiring_inside_the_dte_floor(self):
        # 17 DTE fails a 30-day floor
        assert not is_tradable(contract(100), today=self.TODAY, min_dte=30)

    def test_rejects_a_crossed_market(self):
        assert not is_tradable(contract(100, bid=1.50, ask=1.00), today=self.TODAY)

    def test_a_zero_threshold_disables_that_filter(self):
        wide = contract(100, bid=1.00, ask=2.00)
        assert is_tradable(wide, today=self.TODAY, max_spread_pct=0)


class TestSelection:
    def test_by_delta_picks_the_closest_absolute_delta(self):
        chain = [contract(95, delta=0.70), contract(100, delta=0.50),
                 contract(105, delta=0.31)]
        assert by_delta(chain, 0.30).strike == 105

    def test_by_delta_matches_on_absolute_value_for_puts(self):
        chain = [contract(95, "put", delta=-0.28), contract(90, "put", delta=-0.15)]
        assert by_delta(chain, 0.30).strike == 95

    def test_by_delta_returns_none_when_the_chain_has_no_greeks(self):
        assert by_delta([contract(100), contract(105)], 0.30) is None

    def test_by_strike_picks_the_nearest_listed_strike(self):
        chain = [contract(95), contract(100), contract(105)]
        assert by_strike(chain, 103).strike == 105

    def test_nearest_expiry_picks_the_closest_to_target_dte(self):
        near = contract(100, expiration=date(2026, 9, 4))
        far = contract(100, expiration=date(2026, 10, 16))
        picked = nearest_expiry([near, far], target_dte=30, today=date(2026, 9, 1))
        assert picked == date(2026, 10, 16)


class TestSnapshotParsing:
    SNAP = {
        "latestQuote": {"bp": 2.10, "ap": 2.20},
        "greeks": {"delta": 0.42},
    }

    def test_builds_a_contract_from_a_chain_snapshot(self):
        c = from_snapshot("AAPL260918C00190000", self.SNAP, open_interest=500)
        assert (c.strike, c.kind, c.bid, c.ask, c.delta, c.open_interest) == (
            190.0, "call", 2.10, 2.20, 0.42, 500)

    def test_build_chain_skips_symbols_it_cannot_parse(self):
        chain = build_chain({"AAPL260918C00190000": self.SNAP, "GARBAGE": self.SNAP})
        assert [c.symbol for c in chain] == ["AAPL260918C00190000"]

    def test_missing_greeks_leave_delta_none_rather_than_zero(self):
        c = from_snapshot("AAPL260918C00190000", {"latestQuote": {"bp": 1, "ap": 2}})
        assert c.delta is None

    def test_implied_vol_is_read_from_beside_greeks_not_inside_it(self):
        """Regression: Alpaca returns impliedVolatility as a sibling of greeks.
        Reading it from inside greeks silently yields None for every contract,
        which would make the whole IV/RV strategy stand aside forever."""
        c = from_snapshot("AAPL260918C00190000", {
            "latestQuote": {"bp": 1.0, "ap": 1.1},
            "greeks": {"delta": 0.42},
            "impliedVolatility": 0.2734,
        })
        assert c.implied_vol == pytest.approx(0.2734)

    def test_a_snapshot_without_implied_vol_leaves_it_none(self):
        c = from_snapshot("AAPL260918C00190000", {"latestQuote": {"bp": 1, "ap": 2}})
        assert c.implied_vol is None
