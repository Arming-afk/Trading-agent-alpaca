"""Typed Python wrapper around Alpaca's official CLI (`alpacahq/cli`).

Why a subprocess wrapper instead of a Python SDK: Alpaca ships the CLI as the
tool "built for long-running agent sessions, cron jobs and CI". This agent runs
as a GitHub Actions cron job, so the CLI is the native surface — and every
Alpaca interaction in this project goes through it. That has a property worth
more than convenience for a system that places real orders: **the exact command
is a string**, so it can be logged verbatim, replayed by a human in a terminal,
and diffed. `logs/decisions.jsonl` stores the argv of every order this agent
submits. Nothing is hidden inside an SDK call.

The CLI emits JSON on stdout for both success and failure; failures carry a
non-zero `status` and an `error` message.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from agent import config

logger = logging.getLogger(__name__)

#: Resolution order: explicit env override, then PATH, then the user-local
#: install directory the setup script writes to.
_FALLBACKS = [os.path.expanduser("~/.local/bin/alpaca"), "/opt/homebrew/bin/alpaca"]


class AlpacaCLIError(RuntimeError):
    """The CLI ran but the API rejected the request."""

    def __init__(self, message: str, *, status: int = 0, hint: str = "", argv: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint
        self.argv = argv or []


class AlpacaCLIMissing(RuntimeError):
    """The `alpaca` binary is not installed. See scripts/install_cli.sh."""


def binary() -> str:
    override = os.getenv("ALPACA_CLI_BIN")
    if override and os.path.exists(override):
        return override
    found = shutil.which("alpaca")
    if found:
        return found
    for path in _FALLBACKS:
        if os.path.exists(path):
            return path
    raise AlpacaCLIMissing(
        "alpaca CLI not found. Run scripts/install_cli.sh, or set ALPACA_CLI_BIN."
    )


def _env() -> dict[str, str]:
    """Credentials travel in the environment, never on the command line —
    argv is world-readable in `ps` output and we log it."""
    env = os.environ.copy()
    if config.ALPACA_API_KEY:
        env["ALPACA_API_KEY"] = config.ALPACA_API_KEY
    if config.ALPACA_SECRET_KEY:
        env["ALPACA_SECRET_KEY"] = config.ALPACA_SECRET_KEY
    env["ALPACA_QUIET"] = "1"          # suppress hints/colour so stdout is pure JSON
    env.pop("ALPACA_LIVE", None)       # paper is the CLI default; never opt out of it
    return env


def run(*args: str, timeout: int = 45) -> Any:
    """Run one CLI command and return its parsed JSON.

    Raises AlpacaCLIError on an API-level failure, AlpacaCLIMissing if the
    binary is absent.
    """
    argv = [binary(), *args, "--quiet"]
    logger.debug("alpaca %s", " ".join(args))

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=_env()
        )
    except subprocess.TimeoutExpired as exc:
        raise AlpacaCLIError(f"CLI timed out after {timeout}s", argv=argv) from exc

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()

    if not out:
        raise AlpacaCLIError(err or f"no output (exit {proc.returncode})", argv=argv)

    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AlpacaCLIError(f"non-JSON output: {out[:200]}", argv=argv) from exc

    # Error envelope: {"error": ..., "status": 4xx, "hint": ..., "code": ...}
    if isinstance(payload, dict) and payload.get("error"):
        raise AlpacaCLIError(
            str(payload["error"]),
            status=int(payload.get("status") or 0),
            hint=str(payload.get("hint") or ""),
            argv=argv,
        )

    if proc.returncode != 0:
        raise AlpacaCLIError(err or f"exit {proc.returncode}", argv=argv)

    return payload


def as_argv(*args: str) -> list[str]:
    """The command a human would type to reproduce this call, credentials
    excluded. Stored alongside every order in the decision log."""
    return ["alpaca", *args]


# ── account & market state ───────────────────────────────────────────────────

def account() -> dict:
    return run("account", "get")


def clock() -> dict:
    return run("clock")


def is_market_open() -> bool:
    return bool(clock().get("is_open"))


def positions() -> list[dict]:
    result = run("position", "list")
    return result if isinstance(result, list) else []


def orders(status: str = "open", limit: int = 200) -> list[dict]:
    result = run("order", "list", "--status", status, "--limit", str(limit))
    return result if isinstance(result, list) else []


# ── options: reference data & market data ────────────────────────────────────

def option_contracts(underlying: str, *, expiration_gte: str, expiration_lte: str,
                     contract_type: str | None = None,
                     strike_gte: float | None = None,
                     strike_lte: float | None = None,
                     limit: int = 500) -> list[dict]:
    """`alpaca option contracts` — the contract reference list (open interest,
    strike, expiry). Greeks and quotes come from option_chain()."""
    args = [
        "option", "contracts",
        "--underlying-symbols", underlying,
        "--expiration-date-gte", expiration_gte,
        "--expiration-date-lte", expiration_lte,
        "--limit", str(limit),
    ]
    if contract_type:
        args += ["--type", contract_type]
    if strike_gte is not None:
        args += ["--strike-price-gte", f"{strike_gte:.2f}"]
    if strike_lte is not None:
        args += ["--strike-price-lte", f"{strike_lte:.2f}"]

    payload = run(*args)
    if isinstance(payload, dict):
        return payload.get("option_contracts") or []
    return payload if isinstance(payload, list) else []


def option_chain(underlying: str, *, expiration: str | None = None,
                 contract_type: str | None = None,
                 strike_gte: float | None = None,
                 strike_lte: float | None = None,
                 limit: int = 500) -> dict[str, dict]:
    """`alpaca data option chain` — latest quote, latest trade and greeks keyed
    by OCC contract symbol."""
    args = ["data", "option", "chain", "--underlying-symbol", underlying,
            "--limit", str(limit)]
    if expiration:
        args += ["--expiration-date", expiration]
    if contract_type:
        args += ["--type", contract_type]
    if strike_gte is not None:
        args += ["--strike-price-gte", f"{strike_gte:.2f}"]
    if strike_lte is not None:
        args += ["--strike-price-lte", f"{strike_lte:.2f}"]

    payload = run(*args)
    if isinstance(payload, dict):
        return payload.get("snapshots") or payload.get("option_chain") or {}
    return {}


def latest_stock_quote(symbol: str) -> dict:
    payload = run("data", "latest-quotes", "--symbols", symbol)
    if isinstance(payload, dict):
        quotes = payload.get("quotes") or {}
        return quotes.get(symbol, {})
    return {}


# ── order submission ─────────────────────────────────────────────────────────

def _legs_json(legs: list[dict]) -> str:
    """Serialise legs for `--legs`. Alpaca's multi-leg schema wants
    {symbol, side, ratio_qty, position_intent} per leg, max 4."""
    if not 1 <= len(legs) <= 4:
        raise ValueError(f"multi-leg orders take 1-4 legs, got {len(legs)}")
    return json.dumps([
        {
            "symbol": leg["symbol"],
            "side": leg["side"],
            "ratio_qty": str(leg.get("ratio_qty", 1)),
            "position_intent": leg["position_intent"],
        }
        for leg in legs
    ], separators=(",", ":"))


def submit_mleg(legs: list[dict], qty: int, *, limit_price: float,
                time_in_force: str = "day", dry_run: bool = False) -> dict:
    """Submit a multi-leg (`mleg`) options order — the spread itself.

    `mleg` is submitted as one order, so the legs fill together or not at all.
    That is the whole reason this agent trades spreads through `mleg` rather
    than legging in with separate orders: a partial fill on a defined-risk
    spread is not a defined-risk position.

    A limit price is mandatory. Market orders on multi-leg options cross the
    full width of two spreads and are the fastest way to donate the edge.
    """
    args = [
        "order", "submit",
        "--order-class", "mleg",
        "--type", "limit",
        "--qty", str(qty),
        "--limit-price", f"{limit_price:.2f}",
        "--time-in-force", time_in_force,
        "--legs", _legs_json(legs),
    ]
    if dry_run:
        args.append("--dry-run")
    return run(*args)


def submit_single_option(symbol: str, side: str, qty: int, *,
                         limit_price: float, position_intent: str,
                         time_in_force: str = "day", dry_run: bool = False) -> dict:
    """Single-leg option order — used to close one orphaned leg, not to open."""
    args = [
        "order", "submit",
        "--symbol", symbol,
        "--side", side,
        "--qty", str(qty),
        "--type", "limit",
        "--limit-price", f"{limit_price:.2f}",
        "--time-in-force", time_in_force,
        "--position-intent", position_intent,
    ]
    if dry_run:
        args.append("--dry-run")
    return run(*args)


def cancel_order(order_id: str) -> dict:
    return run("order", "cancel", order_id)


def get_order(order_id: str) -> dict:
    return run("order", "get", order_id)


def stock_bars(symbol: str, *, start: str, timeframe: str = "1Day",
               limit: int = 300, adjustment: str = "split") -> list[dict]:
    """Daily bars for the underlying — the realized-volatility input.

    `--adjustment split` matters: raw prices put a fake ~50% one-day return in
    the series at every split, which would read as a volatility spike.
    """
    payload = run("data", "bars", "--symbol", symbol, "--start", start,
                  "--timeframe", timeframe, "--limit", str(limit),
                  "--adjustment", adjustment)
    if isinstance(payload, dict):
        bars = payload.get("bars")
        if isinstance(bars, dict):
            return bars.get(symbol) or []
        if isinstance(bars, list):
            return bars
    return []
