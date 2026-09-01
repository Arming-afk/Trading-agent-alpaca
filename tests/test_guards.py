"""The two guards that sit outside the volatility model: the earnings calendar
and the LLM advisor.

They have opposite failure modes and are tested for opposite things. The
calendar must never report "clear" when it simply does not know. The advisor
must never stop a trade because it broke.
"""
import json
from datetime import date

import pytest

from agent import advisor as advisor_mod
from agent import earnings

TODAY = date(2026, 9, 2)
EXPIRY = date(2026, 9, 18)


class TestEarningsCalendar:
    def test_an_event_inside_the_holding_period_blocks(self):
        cal = {"NVDA": [date(2026, 9, 10)]}
        result = earnings.check("NVDA", today=TODAY, expiration=EXPIRY, calendar=cal)
        assert result.blocked
        assert result.next_earnings == date(2026, 9, 10)

    def test_an_event_after_expiry_is_clear(self):
        cal = {"NVDA": [date(2026, 11, 18)]}
        result = earnings.check("NVDA", today=TODAY, expiration=EXPIRY, calendar=cal)
        assert result.status == earnings.CLEAR
        assert not result.blocked

    def test_an_event_within_the_blackout_blocks_even_on_a_short_expiry(self):
        """Implied vol runs up into a print, so a debit bought two days before
        one is paying for the run-up whether or not it holds through it."""
        cal = {"NVDA": [date(2026, 9, 3)]}
        result = earnings.check("NVDA", today=TODAY, expiration=date(2026, 9, 4),
                                calendar=cal)
        assert result.blocked
        assert "within" in result.reason

    def test_a_past_event_does_not_block(self):
        cal = {"NVDA": [date(2026, 8, 20)]}
        assert earnings.check("NVDA", today=TODAY, expiration=EXPIRY,
                              calendar=cal).status == earnings.CLEAR

    def test_an_unknown_symbol_is_unknown_and_never_clear(self):
        """The distinction the whole module turns on.

        A symbol with no entry has not been checked. Reporting it as clear
        would let an empty calendar authorise everything, which is exactly the
        failure a calendar is supposed to prevent.
        """
        result = earnings.check("NVDA", today=TODAY, expiration=EXPIRY, calendar={})
        assert result.status == earnings.UNKNOWN
        assert result.status != earnings.CLEAR
        # Unknown does not block either — the jump filter is the other defence.
        assert not result.blocked

    def test_the_shipped_calendar_is_empty_and_says_so(self):
        """Guessed dates are worse than no dates: they authorise trades on
        numbers nobody checked."""
        raw = json.loads(earnings.CALENDAR_PATH.read_text(encoding="utf-8"))
        assert raw["events"] == {}
        assert "_comment" in raw

    def test_a_missing_file_yields_an_empty_calendar_rather_than_raising(self, tmp_path):
        assert earnings.load_calendar(tmp_path / "nope.json") == {}

    def test_a_malformed_file_does_not_stop_the_agent(self, tmp_path):
        bad = tmp_path / "earnings.json"
        bad.write_text("{not json", encoding="utf-8")
        assert earnings.load_calendar(bad) == {}

    def test_bad_dates_are_skipped_and_good_ones_kept(self, tmp_path):
        src = tmp_path / "earnings.json"
        src.write_text(json.dumps(
            {"events": {"nvda": ["2026-11-18", "not-a-date"]}}), encoding="utf-8")
        assert earnings.load_calendar(src) == {"NVDA": [date(2026, 11, 18)]}

    def test_the_next_event_is_the_soonest_one_not_yet_past(self):
        cal = {"NVDA": [date(2026, 5, 1), date(2026, 9, 10), date(2026, 12, 1)]}
        assert earnings.next_event("NVDA", TODAY, calendar=cal) == date(2026, 9, 10)


class TestAdvisorAuthority:
    """It can subtract a trade and do nothing else."""

    def test_it_is_off_without_a_key(self):
        advisor = advisor_mod.Advisor(enabled=False)
        verdict = advisor.review({"symbol": "AAPL"})
        assert verdict.allowed
        assert verdict.status == "disabled"

    def test_a_transport_failure_defers_to_the_rules(self, monkeypatch):
        """Failing open is the deliberate choice.

        Failing closed would hand an API outage the power to halt the strategy,
        which is a larger risk than the one the advisor removes.
        """
        advisor = advisor_mod.Advisor(enabled=True, model="m", api_key="k")
        monkeypatch.setattr(advisor, "_client",
                            lambda: (_ for _ in ()).throw(RuntimeError("timeout")))
        verdict = advisor.review({"symbol": "AAPL"})
        assert verdict.allowed
        assert verdict.status == "error"

    @pytest.mark.parametrize("raw,expected_veto", [
        ('{"veto": true, "reason": "earnings on the 10th"}', True),
        ('{"veto": false, "reason": "nothing scheduled"}', False),
        ('here you go: {"veto": true, "reason": "pending merger"} hope that helps', True),
        ('{"veto": "yes", "reason": "guidance cut"}', True),
    ])
    def test_a_legible_answer_is_honoured(self, raw, expected_veto):
        assert advisor_mod._parse(raw).veto is expected_veto

    @pytest.mark.parametrize("raw", [
        "I cannot help with that.",
        "",
        "{",
        '{"veto": true}',                 # affirmative but unreasoned
        '{"veto": true, "reason": "   "}',
    ])
    def test_an_illegible_answer_is_not_an_objection(self, raw):
        """A veto has to be affirmative and legible.

        Silence, prose and half a JSON object are not objections, and reading
        them as one would let a formatting failure stop the strategy.
        """
        assert advisor_mod._parse(raw).veto is False

    def test_a_veto_carries_its_reason_into_the_log(self):
        verdict = advisor_mod._parse('{"veto": true, "reason": "FOMC on Wednesday"}')
        assert verdict.as_log()["reason"] == "FOMC on Wednesday"
        assert verdict.as_log()["veto"] is True

    def test_an_advisor_that_vetoes_everything_is_reported_as_faulty(self):
        """Indistinguishable, inside one run, from an advisor that is right —
        so the tie is broken by design rather than by trust."""
        advisor = advisor_mod.Advisor(enabled=True)
        advisor.verdicts = [advisor_mod.Verdict(True, "no") for _ in range(4)]
        warning = advisor.sanity_check(considered=4)
        assert warning is not None and "faulty" in warning

    def test_an_ordinary_veto_rate_raises_nothing(self):
        advisor = advisor_mod.Advisor(enabled=True)
        advisor.verdicts = [advisor_mod.Verdict(True, "earnings"),
                            advisor_mod.Verdict(False, "ok"),
                            advisor_mod.Verdict(False, "ok"),
                            advisor_mod.Verdict(False, "ok")]
        assert advisor.sanity_check(considered=4) is None

    def test_the_brief_never_mentions_the_account(self):
        """Knowing the equity invites reasoning about size, and size is not the
        advisor's decision."""
        from agent import spreads, strategy
        from tests.conftest import contract

        spread = spreads.build(spreads.BULL_CALL_DEBIT,
                               [contract(100, "call", bid=5.0, ask=5.1),
                                contract(105, "call", bid=3.0, ask=3.1)])
        regime = strategy.Regime("AAPL", 0.3, 0.2, 1.5, "bullish",
                                 strategy.SELL_PREMIUM, "rich")
        brief = advisor_mod.brief_for(
            strategy.Candidate(regime, spread, "because"), today=TODAY)

        text = json.dumps(brief).lower()
        for forbidden in ("equity", "buying_power", "cash", "budget", "qty",
                          "quantity", "contracts"):
            assert forbidden not in text
