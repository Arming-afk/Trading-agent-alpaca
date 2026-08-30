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
