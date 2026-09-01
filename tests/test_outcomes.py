"""Joining results back to the readings that caused them.

The README promised the strategy was falsifiable from the record. It was not:
every entry logged its IV/RV and nothing ever computed what happened next.
These tests pin the join, and — more importantly — pin the distinctions the
join has to preserve, because collapsing any of them would produce a number
that looks like evidence and is not.
"""
import pytest

from agent import outcomes

LONG = "AAPL260918P00300000"
SHORT = "AAPL260918P00302500"
NVDA_LONG = "NVDA260918C00220000"
NVDA_SHORT = "NVDA260918C00222500"


def opened(symbol, kind, stance, ratio, long_symbol, short_symbol, *,
           order_id="o1", max_loss=1800.0, max_gain=500.0, ts="2026-09-01T14:00:00Z"):
    return {
        "timestamp": ts, "symbol": symbol, "action": "opened",
        "regime": {"stance": stance, "iv_rv_ratio": ratio,
                   "implied_vol": 0.25, "realized_vol": 0.18},
        "spread": {"kind": kind, "underlying": symbol, "expiration": "2026-09-18",
                   "long_leg": long_symbol, "short_leg": short_symbol,
                   "qty": 9, "max_loss": max_loss, "max_gain": max_gain},
        "order": {"id": order_id},
    }


def leg(symbol, qty, pl):
    return {"symbol": symbol, "qty": str(qty), "unrealized_pl": str(pl)}


class TestStatus:
    def test_an_open_position_is_marked_to_market(self):
        rows = outcomes.build(
            [opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT)],
            [leg(LONG, 9, -120), leg(SHORT, -9, 400)], {})
        assert rows[0].status == outcomes.OPEN
        assert rows[0].pl == pytest.approx(280)

    def test_an_order_that_never_filled_is_not_a_break_even_trade(self):
        """Two of the first three live orders expired unfilled.

        Averaging them in as zeros would report a strategy that broke even on
        two thirds of its trades, when in fact two thirds of its trades did not
        happen. That is a fact about the execution path, not about the thesis.
        """
        rows = outcomes.build(
            [opened("QQQ", "bear_put_debit", "buy_premium", 0.81, "L", "S")],
            [], {"o1": {"filled_qty": "0", "status": "expired"}})
        assert rows[0].status == outcomes.UNFILLED
        assert rows[0].resolved is False
        assert rows[0].pl_vs_risk is None

    def test_a_closed_position_reads_its_closing_reason(self):
        decisions = [
            opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT),
            {"timestamp": "2026-09-05T14:00:00Z", "symbol": LONG,
             "action": "closed", "reason": "profit target"},
        ]
        rows = outcomes.build(decisions, [], {"o1": {"filled_qty": "9",
                                                     "status": "filled"}})
        assert rows[0].status == outcomes.CLOSED
        assert "profit target" in rows[0].note

    def test_without_order_history_the_two_collapse_into_unresolved(self):
        """Honest rather than useful — but never silently wrong."""
        rows = outcomes.build(
            [opened("QQQ", "bear_put_debit", "buy_premium", 0.81, "L", "S")],
            [], {})
        assert rows[0].status == outcomes.UNRESOLVED
        assert rows[0].resolved is False


class TestAttribution:
    @pytest.fixture
    def mixed(self):
        decisions = [
            opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT,
                   order_id="credit"),
            opened("NVDA", "bull_call_debit", "buy_premium", 0.63,
                   NVDA_LONG, NVDA_SHORT, order_id="debit", max_loss=2000.0),
        ]
        legs = [leg(LONG, 9, -120), leg(SHORT, -9, 400),
                leg(NVDA_LONG, 16, -200), leg(NVDA_SHORT, -16, -120)]
        return outcomes.build(decisions, legs, {})

    def test_the_two_halves_of_the_strategy_are_never_pooled(self, mixed):
        """They rest on different claims.

        The credit side tests the variance risk premium, which is a documented
        effect. The debit side is a directional bet with a volatility trigger.
        Pooling them lets one hide inside the other, which is precisely what
        the aggregate is supposed to prevent.
        """
        buckets = outcomes.by_stance(mixed)
        assert set(buckets) == {"sell_premium", "buy_premium"}
        assert buckets["sell_premium"].pl == pytest.approx(280)
        assert buckets["buy_premium"].pl == pytest.approx(-320)

    def test_return_is_measured_against_risk_deployed(self, mixed):
        """The only way trades of different sizes compare."""
        assert outcomes.by_stance(mixed)["buy_premium"].return_on_risk == \
            pytest.approx(-0.16)

    def test_the_summary_reports_the_fill_rate(self, mixed):
        assert outcomes.summarise(mixed)["fill_rate"] == pytest.approx(1.0)


class TestVerdict:
    def test_no_resolved_credit_spread_means_untested(self):
        assert "untested" in outcomes.verdict([])

    def test_a_tiny_sample_is_reported_as_a_fact_not_as_evidence(self):
        """Three trades is not weak evidence. It is not evidence."""
        rows = outcomes.build(
            [opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT)],
            [leg(LONG, 9, -120), leg(SHORT, -9, 400)], {})
        text = outcomes.verdict(rows)
        assert "not as evidence" in text
        assert str(outcomes.MIN_SAMPLE_FOR_A_CLAIM) in text

    def test_a_losing_credit_book_at_size_indicts_the_thresholds(self):
        """The stated falsification test, in as many words.

        If credit spreads lose while IV/RV was above the threshold at entry,
        the premium was not rich — which is a verdict on the thresholds, not on
        the direction calls.
        """
        decisions, legs = [], []
        for i in range(outcomes.MIN_SAMPLE_FOR_A_CLAIM):
            long_symbol, short_symbol = f"L{i}", f"S{i}"
            decisions.append(opened("AAPL", "bull_put_credit", "sell_premium",
                                    1.41, long_symbol, short_symbol,
                                    order_id=f"o{i}"))
            legs += [leg(long_symbol, 9, -100), leg(short_symbol, -9, -100)]

        text = outcomes.verdict(outcomes.build(decisions, legs, {}))
        assert "evidence against the thresholds" in text
        assert "not against the direction calls" in text


class TestWrite:
    def test_the_derived_view_is_rewritten_not_appended(self, tmp_path, monkeypatch):
        """It is computed from the journal, so it must never be able to drift
        from it — appending would leave stale rows behind forever."""
        monkeypatch.setattr(outcomes.config, "LOGS", tmp_path)
        rows = outcomes.build(
            [opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT)],
            [leg(LONG, 9, -120), leg(SHORT, -9, 400)], {})

        outcomes.write(rows, outcomes.summarise(rows))
        first = (tmp_path / "outcomes.jsonl").read_text(encoding="utf-8")
        outcomes.write(rows, outcomes.summarise(rows))
        second = (tmp_path / "outcomes.jsonl").read_text(encoding="utf-8")

        assert first.count("\n") == second.count("\n")
