import math

import pytest

from agent import vol
from tests.conftest import contract


class TestRealizedVol:
    def test_a_flat_series_has_zero_volatility(self):
        assert vol.realized_vol([100.0] * 25, window=20) == pytest.approx(0.0)

    def test_annualises_a_known_daily_deviation(self):
        """Alternating +1%/-1% log moves: the sample deviation of the returns
        times sqrt(252) is the annualised figure."""
        closes = [100.0]
        for i in range(30):
            closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
        rv = vol.realized_vol(closes, window=20)
        expected_daily = math.log(1.01)
        # alternating series: |r| is constant, mean ~0, so sd ~ |r|
        assert rv == pytest.approx(expected_daily * math.sqrt(252), rel=0.05)

    def test_returns_none_without_enough_history(self):
        assert vol.realized_vol([100.0, 101.0], window=20) is None

    def test_returns_none_rather_than_zero_so_the_ratio_cannot_blow_up(self):
        """A 0.0 here would make variance_premium infinite instead of unknown."""
        assert vol.realized_vol([], window=20) is None

    def test_ignores_nonpositive_prices(self):
        closes = [100.0] * 15 + [0.0] + [100.0] * 15
        assert vol.realized_vol(closes, window=20) is not None

    def test_uses_only_the_last_window_of_returns(self):
        calm = [100.0] * 40
        stormy = calm + [100.0, 130.0, 90.0, 120.0] * 6
        assert vol.realized_vol(stormy, window=20) > 0.5


class TestVariancePremium:
    def test_is_the_ratio_of_implied_to_realized(self):
        assert vol.variance_premium(0.30, 0.20) == pytest.approx(1.5)

    def test_is_none_when_either_input_is_missing(self):
        assert vol.variance_premium(None, 0.20) is None
        assert vol.variance_premium(0.30, None) is None

    def test_is_none_on_zero_realized_vol_rather_than_dividing_by_zero(self):
        assert vol.variance_premium(0.30, 0.0) is None


class TestATM:
    def test_picks_the_strike_closest_to_spot(self):
        chain = [contract(95), contract(100), contract(105)]
        assert vol.atm_contract(chain, 101).strike == 100

    def test_ignores_contracts_with_no_market(self):
        chain = [contract(100, bid=0, ask=0), contract(105)]
        assert vol.atm_contract(chain, 100).strike == 105

    def test_returns_none_on_an_empty_chain(self):
        assert vol.atm_contract([], 100) is None

    def test_atm_iv_reads_the_iv_of_the_atm_symbol(self):
        chain = [contract(100), contract(105)]
        ivs = {chain[0].symbol: 0.28, chain[1].symbol: 0.31}
        assert vol.atm_iv(chain, 100, ivs) == pytest.approx(0.28)

    def test_atm_iv_is_none_when_that_symbol_has_no_iv(self):
        chain = [contract(100)]
        assert vol.atm_iv(chain, 100, {}) is None


class TestTrendBias:
    def test_reads_bullish_above_the_moving_average(self):
        closes = list(range(80, 120))          # steadily rising
        assert vol.trend_bias(closes, window=20) == "bullish"

    def test_reads_bearish_below_the_moving_average(self):
        closes = list(range(120, 80, -1))
        assert vol.trend_bias(closes, window=20) == "bearish"

    def test_is_neutral_without_enough_history(self):
        assert vol.trend_bias([100.0, 101.0], window=20) == "neutral"
