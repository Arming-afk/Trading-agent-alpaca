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


class TestNettedEntries:
    """Several entries at the same strikes are one position at the broker.

    The report exists to attribute results back to the reading that caused
    them, so it must keep the entries separate — but it must not read the same
    position's P&L once per entry. On 2026-09-02 it did: AAPL 260925 P305/P310
    was entered twice for four contracts, and the credit side's loss was
    reported as $521 when the positions were down $277.
    """

    LONG, SHORT = "AAPL260925P00305000", "AAPL260925P00310000"

    def _rows(self, broker_pl_per_leg: float = -98.0):
        decisions = [
            opened("AAPL", "bull_put_credit", "sell_premium", 1.35,
                   self.LONG, self.SHORT, order_id="a", max_loss=1660.0,
                   ts="2026-09-01T17:23:00Z"),
            opened("AAPL", "bull_put_credit", "sell_premium", 1.35,
                   self.LONG, self.SHORT, order_id="b", max_loss=1648.0,
                   ts="2026-09-02T17:47:00Z"),
        ]
        legs = [leg(self.LONG, 8, broker_pl_per_leg),
                leg(self.SHORT, -8, broker_pl_per_leg)]
        return outcomes.build(decisions, legs, {})

    def test_the_position_pl_is_counted_once_across_entries(self):
        rows = self._rows()
        assert sum(o.pl for o in rows) == pytest.approx(-196.0)

    def test_each_entry_keeps_its_own_reading_and_its_own_risk(self):
        """Splitting the P&L must not collapse the attribution — the reading at
        entry is the only thing this report is for."""
        rows = self._rows()
        assert len(rows) == 2
        assert [o.ratio for o in rows] == [1.35, 1.35]
        assert sorted(o.max_loss for o in rows) == [1648.0, 1660.0]

    def test_the_split_follows_the_share_of_contracts(self):
        rows = self._rows()
        assert rows[0].pl == pytest.approx(-98.0)
        assert rows[1].pl == pytest.approx(-98.0)

    def test_an_uneven_split_follows_the_quantities(self):
        decisions = [
            opened("AAPL", "bull_put_credit", "sell_premium", 1.35,
                   self.LONG, self.SHORT, order_id="a", ts="2026-09-01T17:23:00Z"),
            opened("AAPL", "bull_put_credit", "sell_premium", 1.35,
                   self.LONG, self.SHORT, order_id="b", ts="2026-09-02T17:47:00Z"),
        ]
        decisions[0]["spread"]["qty"] = 3
        decisions[1]["spread"]["qty"] = 9
        legs = [leg(self.LONG, 12, -200.0), leg(self.SHORT, -12, -200.0)]
        rows = outcomes.build(decisions, legs, {})

        assert rows[0].pl == pytest.approx(-100.0)   # 3/12 of -400
        assert rows[1].pl == pytest.approx(-300.0)   # 9/12 of -400
        assert sum(o.pl for o in rows) == pytest.approx(-400.0)

    def test_the_note_says_the_position_was_entered_more_than_once(self):
        assert "entered 2x" in self._rows()[0].note

    def test_a_single_entry_is_untouched(self):
        decisions = [opened("AAPL", "bull_put_credit", "sell_premium", 1.41,
                            LONG, SHORT)]
        rows = outcomes.build(decisions, [leg(LONG, 9, -120), leg(SHORT, -9, 400)], {})
        assert rows[0].pl == pytest.approx(280.0)
        assert "entered" not in rows[0].note

    def test_the_stance_bucket_no_longer_double_counts(self):
        buckets = outcomes.by_stance(self._rows())
        assert buckets["sell_premium"].pl == pytest.approx(-196.0)


class TestOfflineHonesty:
    """A run with no broker view knows what was sent, not what happened.

    Reporting "filled 0, 0% fill rate" from an offline run states as fact the
    one thing that run could not determine. The account had filled all seven.
    """

    def _offline(self):
        return outcomes.build(
            [opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT)],
            [], {})

    def test_counts_that_need_the_broker_are_none_not_zero(self):
        s = outcomes.summarise(self._offline())
        assert s["broker_data"] is False
        assert s["filled"] is None
        assert s["fill_rate"] is None
        assert s["total_pl"] is None

    def test_the_verdict_declines_rather_than_concluding(self):
        assert "knows what was sent" in outcomes.summarise(self._offline())["verdict"]

    def test_the_count_of_what_was_sent_is_still_reported(self):
        """The journal does know that much, and it is the useful half."""
        assert outcomes.summarise(self._offline())["trades_submitted"] == 1

    def test_a_broker_view_restores_the_real_numbers(self):
        rows = outcomes.build(
            [opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT)],
            [leg(LONG, 9, -120), leg(SHORT, -9, 400)], {})
        s = outcomes.summarise(rows)
        assert s["broker_data"] is True
        assert s["filled"] == 1
        assert s["total_pl"] == pytest.approx(280.0)

    def test_one_known_row_is_enough_to_report(self):
        """A partial broker view is still a view — only a total absence of one
        makes the counts unanswerable."""
        rows = outcomes.build(
            [opened("AAPL", "bull_put_credit", "sell_premium", 1.41, LONG, SHORT,
                    order_id="a"),
             opened("QQQ", "bear_put_debit", "buy_premium", 0.81, "L", "S",
                    order_id="b")],
            [leg(LONG, 9, -120), leg(SHORT, -9, 400)], {})
        assert outcomes.summarise(rows)["broker_data"] is True
