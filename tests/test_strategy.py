from datetime import date

import pytest

from agent import spreads, strategy
from agent.strategy import (BUY_PREMIUM, SELL_PREMIUM, STAND_ASIDE, classify,
                            propose, select_legs, spread_kind_for)
from tests.conftest import EXP, contract

TODAY = date(2026, 9, 1)          # 17 DTE to EXP
RISING = list(range(80, 120))
FALLING = list(range(120, 80, -1))


def chain_around(spot: float, kind: str, *, deltas: bool = True,
                 increment: float = 5.0, n: int = 8) -> list:
    """A liquid chain of `n` strikes stepped `increment` apart around spot."""
    out = []
    for i in range(-n // 2, n // 2 + 1):
        strike = spot + i * increment
        if strike <= 0:
            continue
        moneyness = (strike - spot) / spot
        # crude but monotone: delta falls as a call goes further OTM
        d = max(0.02, min(0.98, 0.5 - moneyness * 6))
        if kind == "put":
            d = -max(0.02, min(0.98, 0.5 + moneyness * 6))
        mid = max(0.20, 5.0 - abs(moneyness) * 40)
        out.append(contract(strike, kind, bid=mid - 0.05, ask=mid + 0.05,
                            delta=(d if deltas else None), oi=5000))
    return out


class TestClassify:
    def test_rich_premium_says_sell(self):
        r = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        assert r.stance == SELL_PREMIUM
        assert r.ratio == pytest.approx(1.6)
        assert "rich" in r.reason

    def test_cheap_premium_says_buy(self):
        r = classify("AAPL", implied=0.18, realized=0.25, closes=RISING)
        assert r.stance == BUY_PREMIUM
        assert "cheap" in r.reason

    def test_the_middle_band_stands_aside(self):
        r = classify("AAPL", implied=0.26, realized=0.25, closes=RISING)
        assert r.stance == STAND_ASIDE
        assert "no edge claimed" in r.reason

    def test_the_thresholds_are_inclusive_at_the_boundary(self):
        exactly_rich = classify("AAPL", implied=0.25 * strategy.RICH_RATIO,
                                realized=0.25, closes=RISING)
        assert exactly_rich.stance == SELL_PREMIUM

    def test_missing_data_stands_aside_rather_than_guessing(self):
        r = classify("AAPL", implied=None, realized=0.25, closes=RISING)
        assert r.stance == STAND_ASIDE
        assert r.ratio is None
        assert "missing" in r.reason

    def test_the_reading_is_logged_in_full(self):
        r = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        logged = r.as_log()
        assert logged["iv_rv_ratio"] == pytest.approx(1.6)
        assert logged["implied_vol"] == 0.40 and logged["realized_vol"] == 0.25
        assert logged["stance"] == SELL_PREMIUM


class TestSpreadKind:
    def test_selling_premium_in_an_uptrend_sells_puts(self):
        assert spread_kind_for(SELL_PREMIUM, "bullish") == spreads.BULL_PUT_CREDIT

    def test_selling_premium_in_a_downtrend_sells_calls(self):
        assert spread_kind_for(SELL_PREMIUM, "bearish") == spreads.BEAR_CALL_CREDIT

    def test_a_neutral_trend_still_sells_premium(self):
        """Decay, not direction, is the premium-selling case."""
        assert spread_kind_for(SELL_PREMIUM, "neutral") == spreads.BULL_PUT_CREDIT

    def test_buying_premium_follows_the_trend(self):
        assert spread_kind_for(BUY_PREMIUM, "bullish") == spreads.BULL_CALL_DEBIT
        assert spread_kind_for(BUY_PREMIUM, "bearish") == spreads.BEAR_PUT_DEBIT

    def test_buying_premium_without_a_direction_is_declined(self):
        """A debit spread needs direction; without one it is a coin flip."""
        assert spread_kind_for(BUY_PREMIUM, "neutral") is None

    def test_standing_aside_produces_no_structure(self):
        assert spread_kind_for(STAND_ASIDE, "bullish") is None


class TestSelectLegs:
    def test_a_put_credit_spread_puts_the_protective_leg_lower(self):
        legs = select_legs(spreads.BULL_PUT_CREDIT, chain_around(100, "put"),
                           spot=100, today=TODAY)
        anchor, protective = legs
        assert protective.strike < anchor.strike

    def test_a_call_credit_spread_puts_the_protective_leg_higher(self):
        legs = select_legs(spreads.BEAR_CALL_CREDIT, chain_around(100, "call"),
                           spot=100, today=TODAY)
        anchor, protective = legs
        assert protective.strike > anchor.strike

    def test_the_credit_anchor_is_out_of_the_money(self):
        anchor, _ = select_legs(spreads.BULL_PUT_CREDIT,
                                chain_around(100, "put"), spot=100, today=TODAY)
        assert anchor.strike < 100

    def test_the_debit_anchor_sits_near_the_money(self):
        anchor, _ = select_legs(spreads.BULL_CALL_DEBIT,
                                chain_around(100, "call"), spot=100, today=TODAY)
        assert abs(anchor.strike - 100) <= 5

    def test_falls_back_to_moneyness_when_the_chain_has_no_greeks(self):
        legs = select_legs(spreads.BULL_PUT_CREDIT,
                           chain_around(100, "put", deltas=False),
                           spot=100, today=TODAY)
        assert legs is not None
        anchor, protective = legs
        assert anchor.strike != protective.strike

    def test_picks_only_the_option_type_the_spread_needs(self):
        mixed = chain_around(100, "put") + chain_around(100, "call")
        anchor, protective = select_legs(spreads.BULL_PUT_CREDIT, mixed,
                                         spot=100, today=TODAY)
        assert anchor.kind == protective.kind == "put"

    def test_returns_none_when_the_chain_is_too_thin_to_build_a_spread(self):
        assert select_legs(spreads.BULL_PUT_CREDIT, [contract(100, "put")],
                           spot=100, today=TODAY) is None

    def test_returns_none_on_a_nonsensical_spot(self):
        assert select_legs(spreads.BULL_PUT_CREDIT, chain_around(100, "put"),
                           spot=0, today=TODAY) is None


class TestPropose:
    def test_a_rich_regime_produces_a_credit_spread(self):
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        cand = propose(regime, chain_around(100, "put"), spot=100, today=TODAY)
        assert cand is not None
        assert cand.spread.kind == spreads.BULL_PUT_CREDIT
        assert not cand.spread.is_debit

    def test_a_cheap_regime_in_an_uptrend_produces_a_call_debit_spread(self):
        regime = classify("AAPL", implied=0.18, realized=0.25, closes=RISING)
        cand = propose(regime, chain_around(100, "call"), spot=100, today=TODAY)
        assert cand is not None
        assert cand.spread.kind == spreads.BULL_CALL_DEBIT

    def test_a_downtrend_with_rich_premium_sells_calls(self):
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=FALLING)
        cand = propose(regime, chain_around(100, "call"), spot=100, today=TODAY)
        assert cand.spread.kind == spreads.BEAR_CALL_CREDIT

    def test_standing_aside_proposes_nothing(self):
        regime = classify("AAPL", implied=0.26, realized=0.25, closes=RISING)
        assert propose(regime, chain_around(100, "put"), spot=100, today=TODAY) is None

    def test_an_illiquid_chain_proposes_nothing(self):
        """Every contract quoted 3.00/6.00 — a 66% market — must be rejected
        outright rather than traded at reduced size."""
        wide = [contract(s, "put", bid=3.00, ask=6.00, delta=-0.20, oi=5000)
                for s in (85, 90, 95, 100)]
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        assert propose(regime, wide, spot=100, today=TODAY) is None

    def test_a_chain_outside_the_dte_window_proposes_nothing(self):
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        # EXP is 17 days out; ask for it from a date that makes it 200 DTE
        far_past = date(2026, 3, 1)
        assert propose(regime, chain_around(100, "put"), spot=100,
                       today=far_past) is None

    def test_the_rationale_names_the_regime_and_the_structure(self):
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        cand = propose(regime, chain_around(100, "put"), spot=100, today=TODAY)
        assert "IV/RV" in cand.rationale
        assert spreads.BULL_PUT_CREDIT in cand.rationale

    def test_a_wide_but_passable_market_is_flagged_in_notes(self):
        chain = chain_around(100, "put")
        loose = [contract(c.strike, "put", bid=c.mid * 0.94, ask=c.mid * 1.06,
                          delta=c.delta, oi=5000) for c in chain]
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        cand = propose(regime, loose, spot=100, today=TODAY)
        if cand is not None and cand.spread.worst_spread_pct > 15:
            assert any("wide market" in n for n in cand.notes)

    def test_the_proposed_spread_carries_a_bounded_loss(self):
        regime = classify("AAPL", implied=0.40, realized=0.25, closes=RISING)
        cand = propose(regime, chain_around(100, "put"), spot=100, today=TODAY)
        assert 0 < cand.spread.max_loss < float("inf")
        assert cand.spread.max_loss + cand.spread.max_gain == pytest.approx(
            cand.spread.width * spreads.MULTIPLIER)
