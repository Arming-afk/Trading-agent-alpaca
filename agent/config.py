"""Environment-driven configuration.

Every knob has an a-priori default. None of these were tuned against the
competition track record — the window is five trading days, which is far too
short to distinguish a good threshold from a lucky one.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=ROOT / ".env")

LOGS = ROOT / "logs"
DECISIONS_LOG = LOGS / "decisions.jsonl"
RUNS_LOG = LOGS / "daily_runs.jsonl"


def _num(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.split("#")[0].strip())
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    return int(_num(name, default))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.split("#")[0].strip().lower() in ("1", "true", "yes", "on")


# ── credentials / safety ─────────────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

#: Master switch. When False the agent runs the full decision path and writes
#: every log, but no order request ever leaves the process.
TRADING_ENABLED = _flag("TRADING_ENABLED", False)

# ── universe ─────────────────────────────────────────────────────────────────
UNIVERSE = [
    s.strip().upper()
    for s in os.getenv("UNIVERSE", "SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META").split(",")
    if s.strip()
]

# ── risk gates ───────────────────────────────────────────────────────────────
MAX_DRAWDOWN_PCT = _num("RISK_MAX_DRAWDOWN_PCT", 10.0)
MAX_PORTFOLIO_RISK_PCT = _num("RISK_MAX_PORTFOLIO_RISK_PCT", 25.0)
MAX_TRADE_RISK_PCT = _num("RISK_MAX_TRADE_RISK_PCT", 2.0)
MAX_NEW_TRADES_PER_DAY = _int("RISK_MAX_NEW_TRADES_PER_DAY", 3)

# ── contract selection guardrails ────────────────────────────────────────────
MIN_DTE = _int("MIN_DTE", 7)
MAX_DTE = _int("MAX_DTE", 45)
MAX_SPREAD_PCT = _num("MAX_SPREAD_PCT", 10.0)
MIN_OPEN_INTEREST = _int("MIN_OPEN_INTEREST", 100)

# ── LLM ──────────────────────────────────────────────────────────────────────
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY", "")
