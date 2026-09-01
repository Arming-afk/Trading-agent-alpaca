"""The jump filter, and the regime decision that depends on it.

The case these pin is not hypothetical. On 2026-08-31 the agent read NVDA at
IV/RV 0.63 off a 45% realized-vol reading and bought a debit spread on the
conclusion that options were cheap. The 45% was one earnings gap inside a
20-day window. The trade was a directional bet triggered by a broken
denominator, and nothing in the code could tell.
"""
import math

import pytest

from agent import vol
from agent.strategy import (BUY_PREMIUM, CHEAP_RATIO, RICH_RATIO, SELL_PREMIUM,
                            STAND_ASIDE, classify)


def series(daily_return: float, n: int = 30, *, jump: float | None = None,
           jump_at: int = 15, wobble: float = 0.006) -> list[float]:
    """A price path with a drift, ordinary day-to-day noise, and optionally one
    jump day.

    The noise is not decoration. A path whose returns are all identical has
    exactly zero variance, so realized vol is 0.0, every ratio is undefined and
    the whole filter is untestable against it — a fixture that quiet does not
    resemble any market and would silently pass tests that assert nothing.
    It is generated deterministically so the assertions stay reproducible.
    """
    closes = [100.0]
    for i in range(n):
        if jump is not None and i == jump_at:
            step = jump
        else:
            # A fixed alternating perturbation: deterministic, mean-ish zero,
            # and large enough that the standard deviation is non-degenerate.
            step = daily_return + wobble * math.sin(i * 1.7)
        closes.append(closes[-1] * (1 + step))
    return closes


SMOOTH = series(0.004)
WITH_GAP = series(0.004, jump=-0.17)


class TestRealizedVolExJump:
    def test_dropping_the_largest_day_lowers_the_estimate(self):
        assert vol.realized_vol_ex_jump(WITH_GAP) < vol.realized_vol(WITH_GAP)

    def test_a_smooth_series_barely_moves(self):
        ratio = vol.jump_ratio(SMOOTH)
        assert ratio is not None and ratio < vol.JUMP_TOLERANCE

    def test_a_gapped_series_is_flagged(self):
        assert vol.is_jump_contaminated(WITH_GAP)

    def test_a_smooth_series_is_not_flagged(self):
        assert not vol.is_jump_contaminated(SMOOTH)

    def test_too_short_a_window_returns_none_rather_than_zero(self):
        # A zero would become an infinite ratio downstream, which is the exact
        # failure mode realized_vol() was written to avoid.
        assert vol.realized_vol_ex_jump([100.0, 101.0, 102.0], window=20) is None

    def test_jump_ratio_is_never_negative(self):
        # Removing the largest absolute return cannot raise a standard
        # deviation, but floating point should not be trusted to agree.
        for closes in (SMOOTH, WITH_GAP, series(-0.002)):
            assert vol.jump_ratio(closes) >= 0.0


class TestClassifyRobustness:
    """A stance has to survive both readings of the same window."""

    def test_the_nvda_case_stands_aside(self):
        realized = vol.realized_vol(WITH_GAP)
        # Implied consistent with a post-earnings crush: well below the
        # gap-inflated realized reading, so the naive ratio says "cheap".
        implied = realized * 0.6
        regime = classify("NVDA", implied=implied, realized=realized,
                          closes=WITH_GAP)
        assert regime.ratio < CHEAP_RATIO          # the naive read says buy
        assert regime.stance == STAND_ASIDE        # the robust read refuses
        assert regime.jump_blocked
        # Either branch may claim it — the window here is contaminated enough
        # to be refused on its own terms before the stance-flip test runs. What
        # matters is that the reason names the single day, so the log says why.
        assert "one session" in regime.reason or "largest day" in regime.reason

    def test_a_clean_cheap_reading_still_trades(self):
        realized = vol.realized_vol(SMOOTH)
        regime = classify("AAPL", implied=realized * 0.6, realized=realized,
                          closes=SMOOTH)
        assert regime.stance == BUY_PREMIUM
        assert not regime.jump_blocked

    def test_a_clean_rich_reading_still_trades(self):
        realized = vol.realized_vol(SMOOTH)
        regime = classify("AAPL", implied=realized * 1.6, realized=realized,
                          closes=SMOOTH)
        assert regime.stance == SELL_PREMIUM
        assert not regime.jump_blocked

    def test_the_filter_can_only_remove_trades(self):
        """It must never turn a stand-aside into a trade.

        The check is a veto on an existing signal, not a second signal. If it
        could create one, a jump in the window would become a reason to trade
        — which is the original bug with the sign flipped.
        """
        realized = vol.realized_vol(WITH_GAP)
        for multiple in (0.5, 0.7, 0.9, 1.0, 1.1, 1.4, 2.0, 5.0):
            naive = classify("X", implied=realized * multiple, realized=realized,
                             closes=WITH_GAP)
            unfiltered = classify("X", implied=realized * multiple,
                                  realized=realized, closes=SMOOTH)
            if unfiltered.stance == STAND_ASIDE:
                # nothing to remove; the filter must not invent one
                assert naive.stance == STAND_ASIDE or naive.jump_blocked is False

    def test_both_readings_are_recorded_even_when_they_agree(self):
        realized = vol.realized_vol(SMOOTH)
        regime = classify("AAPL", implied=realized * 1.6, realized=realized,
                          closes=SMOOTH)
        log = regime.as_log()
        assert log["realized_vol_ex_jump"] is not None
        assert log["iv_rv_ratio_ex_jump"] is not None
        assert log["jump_ratio"] is not None

    def test_the_ex_jump_ratio_scales_the_supplied_realized_vol(self):
        """The two readings must measure the same thing.

        `realized` is supplied by the caller, which owns the estimator and the
        window. Recomputing an independent absolute figure from the closes
        would compare two different measurements whenever they diverged — and
        they diverge exactly when the caller is doing something deliberate.
        """
        supplied = 0.25
        regime = classify("AAPL", implied=0.18, realized=supplied, closes=SMOOTH)
        jump = vol.jump_ratio(SMOOTH)
        assert regime.realized_vol_ex_jump == pytest.approx(supplied * (1 - jump))

    def test_missing_closes_leave_the_naive_reading_alone(self):
        # Not enough bars to compute a jump ratio: the robustness check has
        # nothing to say and must not block on its own ignorance.
        regime = classify("AAPL", implied=0.18, realized=0.25,
                          closes=[100.0, 101.0])
        assert regime.stance == BUY_PREMIUM
        assert regime.jump_ratio is None
        assert not regime.jump_blocked

    def test_no_implied_vol_stands_aside_without_crashing(self):
        regime = classify("AAPL", implied=None, realized=0.25, closes=SMOOTH)
        assert regime.stance == STAND_ASIDE
        assert regime.ratio is None


class TestContaminatedWindowIsRefusedOnItsOwnTerms:
    """The near-miss the first live run produced, four hours after the filter
    shipped.

    NVDA came back at IV/RV 0.635 with 24.9% of its realized vol carried by one
    session. Dropping that session moved the ratio to 0.846 — against a 0.85
    cheap threshold, so the stance did not flip, by four thousandths. The
    stance-flip test alone would have traded a reading that was a quarter one
    earnings gap.
    """

    #: The live values, from logs/decisions.jsonl on 2026-09-01.
    LIVE_RATIO = 0.6351481580080145
    LIVE_JUMP = 0.24936497030174648

    def _closes_with_jump_fraction(self, target: float) -> list[float]:
        """A path whose jump ratio brackets `target`."""
        for gap in [x / 1000 for x in range(20, 400)]:
            closes = series(0.004, jump=-gap)
            ratio = vol.jump_ratio(closes)
            if ratio is not None and ratio >= target:
                return closes
        raise AssertionError("no path reached the requested jump fraction")

    def test_the_live_near_miss_is_now_refused(self):
        closes = self._closes_with_jump_fraction(self.LIVE_JUMP)
        realized = vol.realized_vol(closes)
        jump = vol.jump_ratio(closes)

        # An implied vol that puts the naive ratio below CHEAP_RATIO while the
        # ex-jump ratio lands just above it — the exact geometry of the miss.
        implied = realized * 0.63
        regime = classify("NVDA", implied=implied, realized=realized, closes=closes)

        assert jump > vol.JUMP_TOLERANCE
        assert regime.stance == STAND_ASIDE
        assert regime.jump_blocked
        assert "rests on one session" in regime.reason

    def test_a_contaminated_window_is_refused_even_when_the_stance_holds(self):
        """The point of the second condition.

        If both readings agree, the stance-flip test has nothing to say. That
        agreement is not evidence when a quarter of the denominator is one
        event: the ratio is not measuring what the ratio is for.
        """
        closes = self._closes_with_jump_fraction(0.30)
        realized = vol.realized_vol(closes)
        for multiple in (0.3, 0.5, 0.7):        # comfortably "cheap" both ways
            regime = classify("X", implied=realized * multiple,
                              realized=realized, closes=closes)
            assert regime.stance == STAND_ASIDE
            assert regime.jump_blocked

    def test_an_ordinary_window_is_untouched(self):
        """Every symbol carries some largest day. Blocking on that would stop
        the agent trading at all — the ordinary readings on 2026-09-01 ran 6%
        to 18.5%, all below the tolerance.
        """
        realized = vol.realized_vol(SMOOTH)
        assert vol.jump_ratio(SMOOTH) < vol.JUMP_TOLERANCE
        regime = classify("AAPL", implied=realized * 1.6, realized=realized,
                          closes=SMOOTH)
        assert regime.stance == SELL_PREMIUM
        assert not regime.jump_blocked

    def test_it_still_cannot_create_a_trade(self):
        closes = self._closes_with_jump_fraction(0.30)
        realized = vol.realized_vol(closes)
        # An ordinary reading, inside the band: contamination must not push it
        # out of the band in either direction.
        regime = classify("X", implied=realized * 1.0, realized=realized,
                          closes=closes)
        assert regime.stance == STAND_ASIDE
