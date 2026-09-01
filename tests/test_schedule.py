"""The idempotency guard that makes a frequent schedule safe.

GitHub's cron is best-effort. The single 14:00 UTC trigger this project started
with fired once at 19:43 and once not at all, so the workflow now attempts a
run every half hour and relies on the journal to work out which pass should do
the survey. If that guard is wrong in either direction the schedule is worse
than the one it replaced: too strict and the agent never trades, too loose and
it surveys twelve times a day.
"""
import json

import pytest

from agent import journal


@pytest.fixture
def runs_log(tmp_path, monkeypatch):
    path = tmp_path / "daily_runs.jsonl"
    monkeypatch.setattr(journal.config, "RUNS_LOG", path)

    def write(*records):
        path.write_text(
            "\n".join(json.dumps(r) for r in records) + ("\n" if records else ""),
            encoding="utf-8")
    return write


class TestCompletedFullRunToday:
    def test_no_records_means_the_survey_has_not_run(self, runs_log):
        runs_log()
        assert journal.completed_full_run_today("2026-09-02") is False

    def test_a_completed_full_run_is_recognised(self, runs_log):
        runs_log({"date": "2026-09-02", "status": "traded", "mode": "full"})
        assert journal.completed_full_run_today("2026-09-02") is True

    def test_a_run_that_surveyed_and_found_nothing_still_counts(self, runs_log):
        """Standing aside on all eight symbols is the designed outcome, not a
        failed pass — re-surveying because nothing traded would defeat the
        guard on exactly the most common day."""
        runs_log({"date": "2026-09-02", "status": "no_trades", "mode": "full"})
        assert journal.completed_full_run_today("2026-09-02") is True

    def test_a_maintenance_run_does_not_satisfy_the_guard(self, runs_log):
        """Otherwise the first maintenance pass of the day would permanently
        prevent the survey it was supposed to be standing in for."""
        runs_log({"date": "2026-09-02", "status": "traded", "mode": "maintenance"})
        assert journal.completed_full_run_today("2026-09-02") is False

    def test_a_closed_market_does_not_satisfy_the_guard(self, runs_log):
        """A pre-open trigger must not consume the day.

        The 14:00 UTC run can land before the market opens on a late-open day;
        recording that as the day's survey would mean the agent never looked.
        """
        runs_log({"date": "2026-09-02", "status": "market_closed", "mode": "full"})
        assert journal.completed_full_run_today("2026-09-02") is False

    def test_yesterdays_run_does_not_count(self, runs_log):
        runs_log({"date": "2026-09-01", "status": "traded", "mode": "full"})
        assert journal.completed_full_run_today("2026-09-02") is False

    def test_a_record_written_before_modes_existed_is_treated_as_full(self, runs_log):
        """The two records already committed have no `mode` field. Treating a
        missing mode as maintenance would re-survey a day that was already
        traded."""
        runs_log({"date": "2026-08-31", "status": "traded"})
        assert journal.completed_full_run_today("2026-08-31") is True

    def test_runs_today_returns_every_pass(self, runs_log):
        runs_log({"date": "2026-09-02", "status": "traded", "mode": "full"},
                 {"date": "2026-09-02", "status": "no_trades", "mode": "maintenance"},
                 {"date": "2026-09-01", "status": "traded", "mode": "full"})
        assert len(journal.runs_today("2026-09-02")) == 2


class TestDailyCapAcrossPasses:
    """With twelve passes a day, the 3-trades cap has to survive being spread
    across them — it is what stops a frequent schedule becoming frequent
    trading."""

    @pytest.fixture
    def decisions_log(self, tmp_path, monkeypatch):
        path = tmp_path / "decisions.jsonl"
        monkeypatch.setattr(journal.config, "DECISIONS_LOG", path)

        def write(*records):
            body = "\n".join(json.dumps(r) for r in records)
            path.write_text(body + "\n", encoding="utf-8")
        return write

    def test_trades_from_separate_passes_all_count(self, decisions_log):
        decisions_log(
            {"timestamp": "2026-09-02T14:00:01+00:00", "action": "opened"},
            {"timestamp": "2026-09-02T16:30:02+00:00", "action": "opened"},
            {"timestamp": "2026-09-02T18:00:03+00:00", "action": "opened"},
        )
        assert journal.trades_opened_today("2026-09-02") == 3

    def test_declines_and_closes_do_not_consume_the_cap(self, decisions_log):
        decisions_log(
            {"timestamp": "2026-09-02T14:00:01+00:00", "action": "declined"},
            {"timestamp": "2026-09-02T14:00:02+00:00", "action": "closed"},
            {"timestamp": "2026-09-02T14:00:03+00:00", "action": "execution"},
            {"timestamp": "2026-09-02T14:00:04+00:00", "action": "opened"},
        )
        assert journal.trades_opened_today("2026-09-02") == 1

    def test_yesterdays_trades_do_not_consume_todays_cap(self, decisions_log):
        decisions_log({"timestamp": "2026-09-01T14:00:01+00:00", "action": "opened"})
        assert journal.trades_opened_today("2026-09-02") == 0
