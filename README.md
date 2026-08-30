# Options Alpha Agent

An autonomous options trading agent for the **Alpaca AI Trading Agents Hackathon**
(28 Aug – 4 Sept 2026). It runs unattended on a schedule, reads the volatility
surface, and opens defined-risk vertical spreads — submitted as single multi-leg
orders through Alpaca's **official CLI**.

**Paper account:** `PA3KYP101HHG` · created 2026-08-30 · funded $100,000 · options level 3

---

## The thesis

The agent does not predict direction, and says so in the code. It trades a
**structural** claim instead: options are usually priced above the volatility
that subsequently arrives. That gap — the variance risk premium — is one of the
better-documented effects in options markets, and unlike a directional forecast
it does not require being right about where a stock is going.

So the agent measures, per symbol, what the options are pricing (ATM implied
vol) against what the stock has actually been doing (20-day realized vol):

| IV / RV | Reading | Action |
|---|---|---|
| ≥ 1.30 | premium is rich | **sell** a credit spread |
| 0.85 – 1.30 | ordinary | **stand aside** |
| ≤ 0.85 | premium is cheap | **buy** a debit spread |

Standing aside is the common case. In the last dry run across eight symbols, six
stood aside and two traded — which is the intended shape, not a quiet day.

Direction only decides *which side* to sell on. `vol.trend_bias` is a
spot-versus-moving-average read whose entire job is to avoid selling puts into a
sustained decline. It is documented as carrying no claim of edge, because the
predecessor to this project spent eight pre-registered experiments failing to
find one in these same names.

**What would falsify this:** if the credit spreads lose money while IV/RV was
above the threshold at entry, the premium was not actually rich — the
thresholds, not the direction calls, were wrong. Every entry logs its IV, RV,
ratio and regime so that question is answered from the record rather than from
memory.

---

## Design commitments

**1 · Every Alpaca interaction goes through the official CLI.**
There is no Alpaca SDK in `requirements.txt`. [`agent/cli.py`](agent/cli.py) is a
typed wrapper that shells out to `alpaca` and parses its JSON. The payoff is
auditability: the exact command is a string, so it is logged verbatim, replayable
by a human in a terminal, and diffable. Nothing this agent does to the account is
hidden inside a library call. Credentials travel in the subprocess environment,
never in argv — argv is visible to any process via `ps`, and we log it.

**2 · Every position is defined-risk.**
Only two-leg verticals. A vertical's worst case is known exactly, in dollars,
*before* the order is sent (`Vertical.max_loss`). That is what makes the
portfolio gates meaningful — they are arithmetic on a known number, not an
estimate from a model. Both legs ride on one `mleg` order so they fill together
or not at all: a partial fill on a vertical is not a defined-risk position.

**3 · Risk limits are a-priori and are not tuned on the competition.**
Five trading days cannot distinguish a good threshold from a lucky one, and
tuning a limit against the record it is judged by is circular. The one threshold
correction made during the build is documented in `agent/strategy.py` with its
reasoning and its timestamp — it was made before any order was placed, on the
basis of published behaviour rather than results.

---

## Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  .github/workflows/daily-trading.yml   cron 14:00 UTC, Mon–Fri   │
   └────────────────────────────┬─────────────────────────────────────┘
                                ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  scripts/daily_run.py                                            │
   │  clock → account → manage open → survey → rank → gate → submit   │
   └───┬──────────────┬───────────────┬──────────────┬────────────────┘
       ▼              ▼               ▼              ▼
  ┌─────────┐   ┌──────────┐   ┌───────────┐  ┌──────────┐
  │ market  │   │   vol    │   │ strategy  │  │   risk   │
  │ join 2  │──▶│ IV vs RV │──▶│  regime → │─▶│ 4 gates  │
  │ endpts  │   │  ratio   │   │  structure│  │ + sizing │
  └────┬────┘   └──────────┘   └─────┬─────┘  └────┬─────┘
       │                             ▼             │
       │                       ┌───────────┐       │
       │                       │  spreads  │       │
       │                       │ max_loss  │       │
       │                       └─────┬─────┘       │
       └───────────┬─────────────────┴─────────────┘
                   ▼
            ┌─────────────┐        ┌──────────────┐
            │  agent/cli  │───────▶│ journal.py   │
            │ `alpaca` ⚙  │        │ audit trail  │
            └──────┬──────┘        └──────────────┘
                   ▼
        Alpaca Trading API (paper)
```

| Module | Responsibility |
|---|---|
| [`agent/cli.py`](agent/cli.py) | Typed wrapper over the official CLI — command construction, JSON parsing, error envelopes, multi-leg submission. |
| [`agent/market.py`](agent/market.py) | Joins the two chain endpoints (see below) into complete, tradable rows. |
| [`agent/chain.py`](agent/chain.py) | OCC parsing, liquidity gating, strike/delta selection. |
| [`agent/vol.py`](agent/vol.py) | Realized vol, ATM implied vol, the IV/RV ratio, trend bias. |
| [`agent/strategy.py`](agent/strategy.py) | Regime → structure → strikes, with a stated reason for every refusal. |
| [`agent/spreads.py`](agent/spreads.py) | Vertical arithmetic: max loss, max gain, breakeven, sizing. |
| [`agent/risk.py`](agent/risk.py) | Four portfolio gates; an order needs a unanimous pass. |
| [`agent/journal.py`](agent/journal.py) | Append-only decision and run records. |

### Which CLI commands the agent uses

| Purpose | Command |
|---|---|
| Equity, buying power | `alpaca account get` |
| Market open / closed | `alpaca clock` |
| Open positions | `alpaca position list` |
| Contract reference + **open interest** | `alpaca option contracts --underlying-symbols …` |
| Quotes, greeks, **implied vol** | `alpaca data option chain --underlying-symbol …` |
| Realized-vol input | `alpaca data bars --symbol … --adjustment split` |
| **Spread submission** | `alpaca order submit --order-class mleg --legs …` |
| Closing a position | `alpaca position close …` |

> **Neither options endpoint is sufficient alone**, which is worth knowing before
> you build against them: `data option chain` returns quotes, greeks and
> `impliedVolatility` but **no open interest**, while `option contracts` returns
> open interest but **no quotes or greeks**. Filtering on open interest against
> the chain alone rejects every contract — it did here, 62 of 62 SPY strikes with
> markets as tight as 0.2%. `agent/market.py` joins them by OCC symbol.
>
> Two more that cost real debugging time: `impliedVolatility` is a **sibling** of
> `greeks`, not a member of it; and the chain endpoint needs its expiry range
> passed explicitly or it returns whatever fits under `--limit`, which is the
> nearest weeklies — so the liquid monthly is never seen.

---

## Risk gates

Four pure functions stand between a decision and an order.
`risk.evaluate()` runs all of them; one failure blocks the trade and the reason
goes into the log.

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

Positions are closed at 60% of maximum profit, or at 5 DTE — whichever comes
first. Taking profit early on short premium is not timidity: the last of the
credit takes the longest to arrive and carries the same tail risk throughout.

---

## The audit trail

`logs/decisions.jsonl` records **every symbol considered, not only the traded
ones.** A log that keeps two trades and drops six refusals reads as a strategy
that fired twice, when it actually declined six times for stated reasons — and
the refusals are the larger part of this strategy's behaviour. Each record
carries the IV/RV reading, the structure, the risk verdict, and the argv a human
could re-run.

```
SPY    declined  IV/RV=0.944  IV/RV 0.94 inside [0.85, 1.30] — no edge claimed
NVDA   opened    IV/RV=0.742  bear_put_debit 220/217.5 2026-09-18
AAPL   opened    IV/RV=1.454  bull_put_credit 302.5/305 2026-09-18
```

---

## Setup

```bash
git clone https://github.com/Arming-afk/Trading-agent-alpaca.git
cd Trading-agent-alpaca
python3 -m pip install -r requirements.txt

./scripts/install_cli.sh          # official Alpaca CLI, checksum-verified
export PATH="$HOME/.local/bin:$PATH"

cp .env.example .env              # fill in paper keys
python3 scripts/daily_run.py --dry-run --force
```

`TRADING_ENABLED` defaults to `0`: the agent runs the full decision path and
writes every log, but submits nothing. Scheduled workflow runs set it to `1`;
manual dispatches default to a dry run.

## Tests

```bash
python3 -m pytest -q      # 166 tests, no network
```

`subprocess.run` is faked, so the CLI tests assert command construction
(credentials stay in the environment, never argv) and success/failure
discrimination. The spread arithmetic is checked against hand-computed values
plus two invariants that hold across all four vertical types — max loss plus max
gain equals the strike width, and breakeven falls between the strikes — which
catch a sign error in any single formula.

Several tests are regressions pinned to bugs the live API exposed and unit tests
could not have: the implied-vol field position, the two-endpoint join, and legs
drawn from different expiries.

## License

MIT — see [LICENSE](LICENSE).
