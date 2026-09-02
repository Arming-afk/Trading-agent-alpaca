import pytest

from agent import risk
from agent.risk import evaluate, high_water_mark
from agent.spreads import BULL_CALL_DEBIT, build
from tests.conftest import contract


def call(strike, mid):
    return contract(strike, "call", bid=mid - 0.05, ask=mid + 0.05)


class TestDrawdownGate:
    def test_passes_well_below_the_limit(self):
        g = risk.drawdown_gate(97_000, 100_000, max_drawdown_pct=10)
        assert g.passed

    def test_blocks_at_the_limit(self):
        """The limit is inclusive — 10% down with a 10% cap must block."""
        g = risk.drawdown_gate(90_000, 100_000, max_drawdown_pct=10)
        assert not g.passed

    def test_blocks_past_the_limit(self):
        assert not risk.drawdown_gate(85_000, 100_000, max_drawdown_pct=10).passed

    def test_equity_above_the_peak_reports_no_drawdown(self):
        g = risk.drawdown_gate(110_000, 100_000, max_drawdown_pct=10)
        assert g.passed and "0.00%" in g.detail

    def test_a_zero_limit_disables_the_gate(self):
        g = risk.drawdown_gate(10_000, 100_000, max_drawdown_pct=0)
        assert g.passed and g.detail == "disabled"

    def test_missing_equity_blocks_rather_than_failing_open(self):
        """Documented rule 2: unknown state means no new risk."""
        assert not risk.drawdown_gate(None, 100_000, max_drawdown_pct=10).passed
        assert not risk.drawdown_gate(95_000, None, max_drawdown_pct=10).passed


class TestPortfolioRiskGate:
    def test_passes_with_headroom(self):
        assert risk.portfolio_risk_gate(10_000, 100_000, 25).passed

    def test_blocks_at_the_cap(self):
        assert not risk.portfolio_risk_gate(25_000, 100_000, 25).passed

    def test_a_zero_cap_disables_the_gate(self):
        assert risk.portfolio_risk_gate(99_000, 100_000, 0).passed

    def test_missing_equity_blocks(self):
        assert not risk.portfolio_risk_gate(0, None, 25).passed


class TestDailyTradeGate:
    def test_passes_below_the_cap(self):
        assert risk.daily_trade_gate(2, max_new_trades=3).passed

    def test_blocks_at_the_cap(self):
        assert not risk.daily_trade_gate(3, max_new_trades=3).passed

    def test_a_zero_cap_disables_the_gate(self):
        assert risk.daily_trade_gate(99, max_new_trades=0).passed


class TestTradeRiskBudget:
    def test_is_a_percentage_of_equity(self):
        assert risk.trade_risk_budget(100_000, 2) == pytest.approx(2_000)

    def test_is_zero_without_equity(self):
        assert risk.trade_risk_budget(None, 2) == 0.0
        assert risk.trade_risk_budget(0, 2) == 0.0


class TestOpenRisk:
    def test_sums_max_loss_across_open_spreads(self):
        a = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])           # $200
        b = build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)], qty=2)    # $400
        assert risk.open_risk_from_positions([a, b]) == pytest.approx(600.0)

    def test_an_empty_book_carries_no_risk(self):
        assert risk.open_risk_from_positions([]) == 0.0


class TestEvaluate:
    BASE = dict(equity=100_000, high_water_mark=100_000, open_risk=0, trades_today=0)

    def test_a_clean_account_is_allowed_with_a_full_budget(self):
        v = evaluate(**self.BASE, overrides={"max_trade_risk_pct": 2})
        assert v.allowed
        assert v.risk_budget == pytest.approx(2_000)
        assert v.blocked_by == []

    def test_one_failing_gate_blocks_and_names_itself(self):
        v = evaluate(**{**self.BASE, "equity": 85_000},
                     overrides={"max_drawdown_pct": 10})
        assert not v.allowed
        assert v.blocked_by == ["drawdown"]
        assert "drawdown" in v.reason()

    def test_a_blocked_verdict_carries_no_budget(self):
        v = evaluate(**{**self.BASE, "trades_today": 9},
                     overrides={"max_new_trades": 3})
        assert not v.allowed and v.risk_budget == 0.0

    def test_multiple_failures_are_all_reported(self):
        v = evaluate(equity=85_000, high_water_mark=100_000,
                     open_risk=30_000, trades_today=5,
                     overrides={"max_drawdown_pct": 10,
                                "max_portfolio_risk_pct": 25,
                                "max_new_trades": 3})
        assert set(v.blocked_by) == {"drawdown", "portfolio_risk", "daily_trades"}

    def test_budget_is_clipped_to_the_portfolio_headroom(self):
        """With 24% of a 25% cap already used, a 2%-of-equity trade budget must
        shrink to the remaining 1% — otherwise the portfolio cap is breachable
        one trade at a time."""
        v = evaluate(equity=100_000, high_water_mark=100_000,
                     open_risk=24_000, trades_today=0,
                     overrides={"max_trade_risk_pct": 2,
                                "max_portfolio_risk_pct": 25})
        assert v.allowed
        assert v.risk_budget == pytest.approx(1_000)

    def test_no_headroom_blocks_even_when_every_gate_passes(self):
        v = evaluate(equity=100_000, high_water_mark=100_000,
                     open_risk=24_999.99, trades_today=0,
                     overrides={"max_trade_risk_pct": 2,
                                "max_portfolio_risk_pct": 25})
        assert v.risk_budget < 1.0


class TestHighWaterMark:
    def test_is_the_peak_of_the_history(self):
        assert high_water_mark([100, 120, 110]) == 120

    def test_includes_the_current_reading(self):
        assert high_water_mark([100, 120], current=150) == 150

    def test_ignores_nonpositive_and_missing_values(self):
        assert high_water_mark([0, -5, 100]) == 100

    def test_is_none_with_no_data(self):
        assert high_water_mark([]) is None


class TestUnderlyingRiskGate:
    """The gap between the per-trade cap and the portfolio cap.

    Each entry is gated on its own and passes at 2% of equity; nothing was
    watching what several of them added up to in one name. AAPL reached 5.2%
    of the account across two separately-approved entries at identical strikes
    — one position in every sense except the journal's, and one gap down would
    have treated it as such.
    """

    def test_a_name_under_the_limit_passes(self):
        result = risk.underlying_risk_gate("AAPL", 2_000.0, 100_000.0,
                                           max_underlying_risk_pct=5.0)
        assert result.passed
        assert "2.00%" in result.detail

    def test_a_name_at_the_limit_blocks(self):
        result = risk.underlying_risk_gate("AAPL", 5_000.0, 100_000.0,
                                           max_underlying_risk_pct=5.0)
        assert not result.passed
        assert "AAPL" in result.detail

    def test_the_live_concentration_blocks(self):
        """AAPL at $5,176 of $100,098 — 5.17%, over a 5% limit."""
        result = risk.underlying_risk_gate("AAPL", 5_176.0, 100_098.0,
                                           max_underlying_risk_pct=5.0)
        assert not result.passed

    def test_a_different_name_is_unaffected(self):
        """Blocking AAPL must not stop the agent trading SPY."""
        result = risk.underlying_risk_gate("SPY", 2_703.0, 100_098.0,
                                           max_underlying_risk_pct=5.0)
        assert result.passed

    def test_missing_equity_blocks_new_risk(self):
        assert not risk.underlying_risk_gate("AAPL", 1.0, None).passed

    def test_a_zero_limit_disables_the_gate(self):
        assert risk.underlying_risk_gate("AAPL", 99_000.0, 100_000.0,
                                         max_underlying_risk_pct=0).passed

    def test_an_unnamed_underlying_says_so_rather_than_passing_silently(self):
        result = risk.underlying_risk_gate(None, 0.0, 100_000.0)
        assert result.passed
        assert "no underlying supplied" in result.detail


class TestUnderlyingHeadroom:
    """The budget is clipped to the name's remaining room, not only the
    portfolio's — otherwise the cap is breachable one trade at a time, which is
    the same hole the portfolio clip was written to close."""

    def _verdict(self, underlying_risk):
        return risk.evaluate(equity=100_000.0, high_water_mark=100_000.0,
                             open_risk=10_000.0, trades_today=0,
                             underlying="AAPL", underlying_risk=underlying_risk,
                             overrides={"max_underlying_risk_pct": 5.0,
                                        "max_trade_risk_pct": 2.0})

    def test_an_empty_name_gets_the_full_per_trade_budget(self):
        assert self._verdict(0.0).risk_budget == pytest.approx(2_000.0)

    def test_a_nearly_full_name_gets_only_what_is_left(self):
        # 5% of 100k is 5,000; 4,200 is on, so 800 remains — less than the
        # 2,000 the per-trade cap would otherwise allow.
        assert self._verdict(4_200.0).risk_budget == pytest.approx(800.0)

    def test_a_full_name_is_blocked_outright(self):
        verdict = self._verdict(5_000.0)
        assert not verdict.allowed
        assert "underlying_risk" in verdict.blocked_by

    def test_the_gate_is_named_in_the_log_when_it_blocks(self):
        assert "AAPL already carries" in self._verdict(5_000.0).reason()

    def test_it_never_closes_anything(self):
        """Like the drawdown breaker, it blocks new risk and leaves open
        spreads alone: they are defined-risk and already paid for."""
        verdict = self._verdict(5_000.0)
        assert verdict.risk_budget == 0.0


class TestRiskByUnderlying:
    def test_entries_in_one_name_are_added_together(self):
        from agent.positions import OpenSpread, risk_by_underlying
        spreads = [
            OpenSpread("AAPL", "bull_put_credit", None, "a", "b", 8, 3308.0, 692.0),
            OpenSpread("AAPL", "bull_put_credit", None, "c", "d", 9, 1868.0, 382.0),
            OpenSpread("SPY", "bear_call_credit", None, "e", "f", 2, 1344.0, 256.0),
        ]
        totals = risk_by_underlying(spreads)
        assert totals["AAPL"] == pytest.approx(5176.0)
        assert totals["SPY"] == pytest.approx(1344.0)
