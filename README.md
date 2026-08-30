# Options Alpha Agent

An autonomous options trading agent for the **Alpaca AI Trading Agents Hackathon**
(28 Aug – 4 Sept 2026). It runs unattended on a schedule, selects defined-risk
vertical spreads, sizes them against hard portfolio limits, and submits them as
single multi-leg orders through Alpaca's **official CLI**.

> Paper trading only. Every order path is gated behind `TRADING_ENABLED`, and
> the CLI's live-trading opt-in is explicitly stripped from the environment.

---

## Design commitments

Three constraints shape everything else. They are stated up front because they
are what the code is actually organised around.

**1 · Every Alpaca interaction goes through the official CLI.**
There is no Python SDK in `requirements.txt`. `agent/cli.py` is a typed wrapper
that shells out to `alpaca` and parses its JSON. The payoff is auditability: the
exact command is a string, so it is logged verbatim, replayable by a human in a
terminal, and diffable. Nothing this agent does to the account is hidden inside
a library call.

**2 · Every position is defined-risk.**
The agent only opens two-leg verticals. A vertical's worst case is known exactly,
in dollars, *before* the order is sent — see `Vertical.max_loss`. This is what
makes the portfolio gates meaningful: they are arithmetic on a known number, not
estimates from a model. An undefined-risk structure would turn every limit in
`agent/risk.py` into a guess.

**3 · Risk limits are a-priori and are not tuned on the competition.**
Five trading days cannot distinguish a good threshold from a lucky one. Tuning a
risk limit against the record it is being judged by is circular, so the defaults
were fixed before the first trade and left alone.

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
   scheduled run ──▶│  scripts/daily_run.py                   │
                    └────────────────┬────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
  ┌───────────┐              ┌──────────────┐             ┌──────────────┐
  │ agent/    │              │  agent/      │             │  agent/      │
  │ chain.py  │  contracts   │  spreads.py  │  structure  │  risk.py     │
  │           │─────────────▶│              │────────────▶│              │
  │ liquidity │              │ defined-risk │             │ 4 gates +    │
  │ filters   │              │ arithmetic   │             │ sizing       │
  └─────┬─────┘              └──────────────┘             └──────┬───────┘
        │                                                        │
        │           ┌────────────────────────────────┐           │
        └──────────▶│        agent/cli.py            │◀──────────┘
                    │  typed wrapper over `alpaca`   │
                    └────────────────┬───────────────┘
                                     ▼
                         Alpaca Trading API (paper)
```

| Module | Responsibility |
|---|---|
| [`agent/cli.py`](agent/cli.py) | Subprocess wrapper over the official CLI. Command construction, JSON parsing, error envelope handling, multi-leg submission. |
| [`agent/chain.py`](agent/chain.py) | OCC symbol parsing, chain assembly, liquidity gating, strike/delta selection. |
| [`agent/spreads.py`](agent/spreads.py) | Vertical construction and its exact risk arithmetic — max loss, max gain, breakeven, sizing. |
| [`agent/risk.py`](agent/risk.py) | Four independent portfolio gates. An order is submitted only on a unanimous pass. |
| [`agent/config.py`](agent/config.py) | Environment-driven configuration with a-priori defaults. |

### Which CLI commands the agent uses

| Purpose | Command |
|---|---|
| Account equity, buying power | `alpaca account get` |
| Market open / closed | `alpaca clock` |
| Open positions | `alpaca position list` |
| Contract reference data | `alpaca option contracts --underlying-symbols …` |
| Quotes + greeks | `alpaca data option chain --underlying-symbol …` |
| **Spread submission** | `alpaca order submit --order-class mleg --legs …` |

Spreads are submitted as one `mleg` order rather than two single-leg orders, so
the legs fill together or not at all. A partial fill on a defined-risk spread is
not a defined-risk position.

---

## Risk gates

Four pure functions stand between a decision and an order. `risk.evaluate()`
runs all of them; a single failure blocks the trade and the reason is written to
the decision log.

| Gate | Default | What it prevents |
|---|---|---|
| `drawdown` | 10% | New positions while equity is far below its peak. |
| `portfolio_risk` | 25% | Twenty 2% trades quietly becoming a 40% bet. |
| `daily_trades` | 3/day | A bug or a news day turning into correlated size. |
| per-trade budget | 2% | Any one spread dominating the book. |

Two rules the tests enforce:

- **Missing data blocks new risk.** Unlike a monitoring system, where a missing
  datum should mean "do not intervene", here the gate is the only thing sizing
  the position — so unknown equity means no trade, not an unsized one.
- **The per-trade budget is clipped to portfolio headroom**, or the portfolio cap
  would be breachable one trade at a time.

---

## Setup

```bash
git clone <this repo> && cd alpaca-options-agent
python3 -m pip install -r requirements.txt

# Install the official Alpaca CLI (checksum-verified)
./scripts/install_cli.sh
export PATH="$HOME/.local/bin:$PATH"

cp .env.example .env      # then fill in the paper keys
alpaca doctor             # verify connectivity
```

`TRADING_ENABLED` defaults to `0`: the agent runs the full decision path and
writes every log, but submits nothing. Watch one run before flipping it.

## Tests

```bash
python3 -m pytest -q
```

107 tests, no network. The spread arithmetic is checked against hand-computed
values and two invariants that hold across all four vertical types: max loss
plus max gain equals the strike width, and breakeven always falls between the
strikes.

---

## Status

Foundation complete and tested: CLI wrapper, chain handling, spread arithmetic,
risk gates. Strategy layer and daily runner in progress.

## License

MIT — see [LICENSE](LICENSE).
