import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.chain import Contract  # noqa: E402

EXP = date(2026, 9, 18)


def contract(strike: float, kind: str = "call", *, bid: float = 1.00,
             ask: float = 1.10, delta: float | None = None,
             iv: float | None = None,
             oi: int = 1000, expiration: date = EXP,
             underlying: str = "AAPL") -> Contract:
    cp = "C" if kind == "call" else "P"
    symbol = f"{underlying}{expiration:%y%m%d}{cp}{int(round(strike * 1000)):08d}"
    return Contract(symbol=symbol, underlying=underlying, expiration=expiration,
                    strike=strike, kind=kind, bid=bid, ask=ask, delta=delta,
                    implied_vol=iv, open_interest=oi)


@pytest.fixture
def make_contract():
    return contract
