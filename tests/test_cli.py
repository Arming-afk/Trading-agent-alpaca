"""The CLI wrapper. These tests never touch the network — subprocess.run is
replaced — so they assert the two things that actually matter: that we build
the right command, and that we correctly tell success from failure."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent import cli
from agent.cli import AlpacaCLIError, _legs_json
from agent.spreads import BULL_CALL_DEBIT, build
from tests.conftest import contract


def call(strike, mid):
    return contract(strike, "call", bid=mid - 0.05, ask=mid + 0.05)


@pytest.fixture
def fake_run(monkeypatch):
    """Capture argv/env and return a canned CLI response."""
    calls = []

    def factory(stdout="{}", stderr="", returncode=0):
        def _run(argv, **kwargs):
            calls.append(SimpleNamespace(argv=argv, env=kwargs.get("env") or {}))
            return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
        monkeypatch.setattr(subprocess, "run", _run)
        return calls

    monkeypatch.setattr(cli, "binary", lambda: "/fake/alpaca")
    return factory


class TestRun:
    def test_parses_json_stdout(self, fake_run):
        fake_run(stdout='{"equity": "100000"}')
        assert cli.run("account", "get") == {"equity": "100000"}

    def test_raises_on_the_error_envelope(self, fake_run):
        fake_run(stdout=json.dumps(
            {"error": "insufficient buying power", "status": 403, "hint": "fund it"}))
        with pytest.raises(AlpacaCLIError) as exc:
            cli.run("order", "submit")
        assert exc.value.status == 403
        assert exc.value.hint == "fund it"

    def test_raises_on_empty_output(self, fake_run):
        fake_run(stdout="", stderr="boom", returncode=1)
        with pytest.raises(AlpacaCLIError, match="boom"):
            cli.run("clock")

    def test_raises_on_non_json_output(self, fake_run):
        fake_run(stdout="not json at all")
        with pytest.raises(AlpacaCLIError, match="non-JSON"):
            cli.run("clock")

    def test_a_nonzero_exit_with_valid_json_still_raises(self, fake_run):
        fake_run(stdout='{"ok": true}', stderr="segfault", returncode=2)
        with pytest.raises(AlpacaCLIError):
            cli.run("clock")

    def test_a_timeout_becomes_a_cli_error(self, monkeypatch):
        monkeypatch.setattr(cli, "binary", lambda: "/fake/alpaca")

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="alpaca", timeout=45)

        monkeypatch.setattr(subprocess, "run", _boom)
        with pytest.raises(AlpacaCLIError, match="timed out"):
            cli.run("clock")


class TestCommandConstruction:
    def test_quiet_is_always_appended_so_stdout_stays_pure_json(self, fake_run):
        calls = fake_run(stdout="{}")
        cli.run("account", "get")
        assert calls[0].argv[-1] == "--quiet"

    def test_credentials_travel_in_the_environment_not_argv(self, fake_run, monkeypatch):
        """argv is visible to any process via `ps`, and we log it verbatim."""
        monkeypatch.setattr(cli.config, "ALPACA_API_KEY", "PKSECRET")
        monkeypatch.setattr(cli.config, "ALPACA_SECRET_KEY", "shhh")
        calls = fake_run(stdout="{}")
        cli.run("account", "get")
        assert "PKSECRET" not in " ".join(calls[0].argv)
        assert calls[0].env["ALPACA_API_KEY"] == "PKSECRET"

    def test_live_trading_is_never_opted_into(self, fake_run, monkeypatch):
        monkeypatch.setenv("ALPACA_LIVE", "1")
        calls = fake_run(stdout="{}")
        cli.run("account", "get")
        assert "ALPACA_LIVE" not in calls[0].env

    def test_as_argv_is_a_reproducible_human_command(self):
        assert cli.as_argv("order", "list") == ["alpaca", "order", "list"]


class TestOptionQueries:
    def test_chain_passes_the_underlying_and_filters(self, fake_run):
        calls = fake_run(stdout='{"snapshots": {}}')
        cli.option_chain("AAPL", expiration="2026-09-18", contract_type="call")
        argv = calls[0].argv
        assert argv[1:5] == ["data", "option", "chain", "--underlying-symbol"]
        assert "--expiration-date" in argv and "2026-09-18" in argv
        assert "--type" in argv and "call" in argv

    def test_chain_unwraps_the_snapshots_envelope(self, fake_run):
        fake_run(stdout='{"snapshots": {"AAPL260918C00190000": {"greeks": {}}}}')
        assert list(cli.option_chain("AAPL")) == ["AAPL260918C00190000"]

    def test_contracts_unwraps_the_option_contracts_envelope(self, fake_run):
        fake_run(stdout='{"option_contracts": [{"symbol": "X"}]}')
        got = cli.option_contracts("AAPL", expiration_gte="2026-09-01",
                                   expiration_lte="2026-10-01")
        assert got == [{"symbol": "X"}]

    def test_positions_returns_a_list_even_on_an_unexpected_shape(self, fake_run):
        fake_run(stdout='{"unexpected": true}')
        assert cli.positions() == []


class TestLegsSerialisation:
    def test_serialises_the_four_fields_alpaca_expects(self):
        legs = json.loads(_legs_json([
            {"symbol": "AAPL260918C00190000", "side": "buy",
             "position_intent": "buy_to_open"}]))
        assert legs[0] == {"symbol": "AAPL260918C00190000", "side": "buy",
                           "ratio_qty": "1", "position_intent": "buy_to_open"}

    def test_ratio_qty_is_stringified(self):
        legs = json.loads(_legs_json([
            {"symbol": "X", "side": "buy", "ratio_qty": 2,
             "position_intent": "buy_to_open"}]))
        assert legs[0]["ratio_qty"] == "2"

    def test_rejects_more_than_four_legs(self):
        leg = {"symbol": "X", "side": "buy", "position_intent": "buy_to_open"}
        with pytest.raises(ValueError, match="1-4 legs"):
            _legs_json([leg] * 5)

    def test_rejects_an_empty_leg_list(self):
        with pytest.raises(ValueError, match="1-4 legs"):
            _legs_json([])


class TestSubmitMleg:
    @pytest.fixture
    def spread(self):
        return build(BULL_CALL_DEBIT, [call(100, 5.00), call(105, 3.00)])

    def test_submits_as_a_single_mleg_limit_order(self, fake_run, spread):
        calls = fake_run(stdout='{"id": "abc"}')
        cli.submit_mleg(spread.legs_payload(), qty=2, limit_price=2.10)
        argv = calls[0].argv
        assert "--order-class" in argv and "mleg" in argv
        assert "--type" in argv and "limit" in argv
        assert argv[argv.index("--limit-price") + 1] == "2.10"
        assert argv[argv.index("--qty") + 1] == "2"

    def test_both_legs_ride_on_one_order(self, fake_run, spread):
        calls = fake_run(stdout='{"id": "abc"}')
        cli.submit_mleg(spread.legs_payload(), qty=1, limit_price=2.10)
        argv = calls[0].argv
        legs = json.loads(argv[argv.index("--legs") + 1])
        assert len(legs) == 2
        assert {l["side"] for l in legs} == {"buy", "sell"}

    def test_dry_run_adds_the_flag(self, fake_run, spread):
        calls = fake_run(stdout='{"id": "abc"}')
        cli.submit_mleg(spread.legs_payload(), qty=1, limit_price=2.10, dry_run=True)
        assert "--dry-run" in calls[0].argv

    def test_a_rejected_order_surfaces_the_api_message(self, fake_run, spread):
        fake_run(stdout=json.dumps({"error": "market closed", "status": 422}))
        with pytest.raises(AlpacaCLIError, match="market closed"):
            cli.submit_mleg(spread.legs_payload(), qty=1, limit_price=2.10)
