# Options Alpha Agent — one-page write-up

**Alpaca AI Trading Agents Hackathon · 28 Aug – 4 Sept 2026**
Paper account `PA3KYP101HHG` · created 2026-08-30 · $100,000 · options level 3
Repository: <https://github.com/Arming-afk/Trading-agent-alpaca>

---

## AI logic

The agent does not forecast direction. Its predecessor spent eight
pre-registered experiments looking for a directional edge in large-cap equities
and graded every one NOT SUPPORTED, so building this entry on one would repeat a
finished experiment.

It trades a **structural** claim instead: options are usually priced above the
volatility that subsequently arrives. Each scheduled run measures, per symbol,
at-the-money implied volatility from the option chain against 20-day realized
volatility from split-adjusted daily bars, and acts on the ratio:

| IV / RV | Reading | Structure |
|---|---|---|
| ≥ 1.30 | premium rich | credit spread — bull put, or bear call in a downtrend |
| 0.85 – 1.30 | ordinary | **stand aside** |
| ≤ 0.85 | premium cheap | debit spread in the direction of trend |

Standing aside is the common case: in the dry runs, six of eight symbols
declined. Direction enters only to choose *which side* to sell — a
spot-versus-moving-average read whose sole job is to avoid selling puts into a
sustained decline. It is documented in the code as carrying no claim of edge.

Thresholds are a-priori, set from the published behaviour of the variance risk
premium rather than from results. One correction was made during the build,
before any order was placed: the first pass used 0.95 as "cheap", which is
roughly the middle of the normal range, and six of eight symbols traded on the
claim that premium was unusually cheap when it was merely unremarkable. The
reasoning and its timing are recorded in `agent/strategy.py`. They are not
revisited on the strength of the competition's P&L — tuning a threshold against
the record it is judged by is circular.

**What would falsify the thesis:** if credit spreads lose money while IV/RV was
above the threshold at entry, the premium was not actually rich. Every entry
logs its IV, RV, ratio and regime so the question is settled from the record.

## Risk gates

Every position is a **two-leg vertical**, so its worst case is known exactly, in
dollars, before the order is sent. That is what makes the portfolio gates
arithmetic on a known number rather than an estimate from a model. Both legs
ride on one `mleg` order — a partial fill on a vertical is not a defined-risk
position.

Four pure functions run on every trade; one failure blocks it and the reason is
logged:

| Gate | Limit | Prevents |
|---|---|---|
| Drawdown breaker | 10% below the high-water mark | Trading into a decline |
| Portfolio risk | 25% of equity across open spreads | Twenty 2% trades becoming a 40% bet |
| Daily trades | 3 | A bug or news day becoming correlated size |
| Per-trade budget | 2% of equity | One spread dominating the book |

Two rules the tests enforce: **missing data blocks new risk** (unknown equity
means no trade, not an unsized one), and **the per-trade budget is clipped to
portfolio headroom**, or the portfolio cap would be breachable one trade at a
time. Contracts are rejected outright — never sized down — when the bid/ask
exceeds 10% of mid or open interest is under 100. Positions close at 60% of max
profit or 5 DTE, whichever comes first.

## Alpaca infrastructure

**Every interaction with Alpaca goes through the official CLI** (`alpacahq/cli`
v0.0.14). There is no Alpaca SDK in `requirements.txt`. `agent/cli.py` wraps it
as typed calls over subprocess and JSON, which buys auditability: the exact
command is a string, so it is logged verbatim, replayable in a terminal, and
diffable. Credentials travel in the subprocess environment, never in argv, which
is visible via `ps` and which we log.

| Purpose | Command |
|---|---|
| Equity, clock, positions | `alpaca account get` · `alpaca clock` · `alpaca position list` |
| Contract reference + open interest | `alpaca option contracts` |
| Quotes, greeks, implied vol | `alpaca data option chain` |
| Realized-vol input | `alpaca data bars --adjustment split` |
| **Spread submission** | `alpaca order submit --order-class mleg --legs …` |

`.github/workflows/daily-trading.yml` runs the agent at 14:00 UTC on weekdays —
half an hour after the open, so quotes are real rather than overnight stubs —
and commits the day's records back to the repository.

**Three API behaviours worth publishing**, each of which cost real debugging:

1. Neither options endpoint is sufficient alone. `data option chain` returns
   quotes, greeks and implied volatility but **no open interest**; `option
   contracts` returns open interest but **no quotes or greeks**. Filtering on
   open interest against the chain alone rejected 62 of 62 SPY strikes whose
   markets were as tight as 0.2%. `agent/market.py` joins them by OCC symbol.
2. `impliedVolatility` is a **sibling** of `greeks`, not a member of it. Reading
   it from inside `greeks` yields None for every contract and the strategy
   stands aside forever.
3. The chain endpoint returns whatever fits under `--limit` — the nearest
   weeklies — unless the expiry range is passed explicitly, so the liquid
   monthly is never seen and strike selection is left choosing among thin
   contracts.

## Verification

166 tests, no network: `subprocess.run` is faked, so the CLI tests assert command
construction and success/failure discrimination. Spread arithmetic is checked
against hand-computed values plus two invariants that hold across all four
vertical types — max loss plus max gain equals the strike width, and breakeven
falls between the strikes. Several tests are regressions pinned to bugs the live
API exposed and unit tests alone could not have caught, including legs drawn from
two different expiries — which is not a vertical at all, and would have made
`max_loss` a fiction that every risk gate then sized against.
