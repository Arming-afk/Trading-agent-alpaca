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
volatility that subsequently arrives. Each pass measures, per symbol,
at-the-money implied volatility from the option chain against 20-day realized
volatility from split-adjusted daily bars, and acts on the ratio:

| IV / RV | Reading | Structure |
|---|---|---|
| ≥ 1.30 | premium rich | credit spread — bull put, or bear call in a downtrend |
| 0.85 – 1.30 | ordinary | **stand aside** |
| ≤ 0.85 | premium cheap | debit spread in the direction of trend |

Standing aside is the common case. Direction enters only to choose *which side*
to sell — a spot-versus-moving-average read whose sole job is to avoid selling
puts into a sustained decline. It is documented in the code as carrying no claim
of edge.

Thresholds are a-priori, set from the published behaviour of the variance risk
premium rather than from results, and are not revisited on the strength of the
competition's P&L — tuning a threshold against the record it is judged by is
circular.

**The denominator can break, and that is the interesting part.** Close-to-close
realized vol cannot tell a jump from volatility. One earnings gap inside the
20-day window inflates it, the ratio reads low, and options look cheap — while
implied has already been crushed by the same event. Both errors point the same
way, so the agent is most likely to buy premium exactly when premium is least
worth buying. It did: NVDA, 2026-08-31, IV/RV 0.63 off a 45% realized reading.

So the window is now measured twice, once with its largest single return
removed, and a stance must clear both a **contamination** test (no more than 20%
of the reading resting on one session) and a **robustness** test (dropping that
day must not change the bucket). Both readings are logged whether or not they
changed anything. The check can only ever remove a trade. A curated earnings
calendar blocks a known print inside the holding period; it ships empty on
purpose, because guessed dates would authorise trades on numbers nobody checked,
and an unlisted symbol is reported as `unknown` rather than clear.

An **LLM advisor** runs last and can only veto — never propose, size, or
overturn a gate. Its brief excludes equity and quantity, because knowing the
account invites reasoning about size and size is not its decision. It fails
open: an outage must not be able to halt the strategy.

**What would falsify the thesis:** if credit spreads lose money while IV/RV was
above the threshold at entry, the premium was not rich. `scripts/outcomes.py`
joins every entry back to its result and answers that from the record — or
declines to, in as many words, when the sample is too small to distinguish the
thesis from noise.

## Risk gates

Every position is a **two-leg vertical**, so its worst case is known exactly, in
dollars, before the order is sent. Both legs ride on one `mleg` order — a
partial fill on a vertical is not a defined-risk position.

| Gate | Limit | Prevents |
|---|---|---|
| Drawdown breaker | 10% below the high-water mark | Trading into a decline |
| Portfolio risk | 25% of equity across open spreads | Twenty 2% trades becoming a 40% bet |
| Underlying risk | 5% of equity in one name | Approved entries stacking at the same strikes |
| Daily trades | 3 | A bug or news day becoming correlated size |
| Per-trade budget | 2% of equity | One spread dominating the book |

Five rules the tests enforce: **missing data blocks new risk**; **every budget
is clipped to the headroom of every cap it shares**, since a cap that only
blocks the trade which crosses it is breachable one trade at a time; **sizing
uses the price the order is sent at, not the midpoint** — the concession toward
the marketable side is always in the direction of more risk; **open risk is
each spread's recorded worst case**, joined from the journal, not the sum of
the legs' cost bases; and **entries at the same strikes are one position**, in
the risk total and in the outcome report alike, because that is what the broker
holds.
Contracts are rejected outright — never sized down — when the bid/ask exceeds
10% of mid or open interest is under 100. Positions close at 60% of the
*package's* max profit or 5 DTE, in one order.

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
| Chase and withdrawal | `alpaca order get --order-id` · `alpaca order cancel --order-id` |

**Four API behaviours worth publishing**, each of which cost real debugging:

1. Neither options endpoint is sufficient alone. `data option chain` returns
   quotes, greeks and implied volatility but **no open interest**; `option
   contracts` returns open interest but **no quotes or greeks**. Filtering on
   open interest against the chain alone rejected 62 of 62 SPY strikes whose
   markets were as tight as 0.2%. `agent/market.py` joins them by OCC symbol.
2. `impliedVolatility` is a **sibling** of `greeks`, not a member of it.
3. The chain endpoint returns whatever fits under `--limit` — the nearest
   weeklies — unless the expiry range is passed explicitly.
4. `order get` and `order cancel` take the id as **`--order-id`**, not
   positionally, and report the rejection with `"status": 0` — a zero status on
   a failed call, which reads like a warning and is not one.

**The schedule is not reliable, and the agent is built for that.** GitHub's cron
is best-effort: the original single 14:00 UTC trigger fired once at 19:43 —
seventeen minutes before the close — and on the next session not at all. The run
is now attempted every thirty minutes and made idempotent; the first pass to
land surveys, the rest manage positions and chase fills. A heartbeat workflow
fails after the close if a session produced no run record, and a failed workflow
is the notification.

## What the live account taught us

Seven defects survived 175 unit tests and a dry run. All four needed a real
account, and they are in the README with their fixes because the failure modes
transfer better than the strategy does.

The sharpest was an execution failure. `order cancel` rejected a positional id;
during a chased run six cancels failed while the chase sent a more aggressive
replacement after each one, and all of them filled. The account ended up
carrying six SPY spreads against an approved two and twelve AAPL against four —
4.0% and 5.0% of equity against a 2% cap. The flag was the trigger; the bug was
that a re-quote could proceed on an unconfirmed cancel at all. A re-quote now
refuses to send a replacement until the original is known to be gone,
`OpenSpread.excess_qty` makes the breach visible at all — the broker reports a
position and nothing about how large it was meant to be — and `ops.py trim`
closed exactly the excess. The account was inside every limit thirteen minutes
later, and the entire sequence, including the breach, is in the committed log.

## Verification

296 tests, no network: `subprocess.run` is faked, so the CLI tests assert command
construction and success/failure discrimination. Spread arithmetic is checked
against hand-computed values plus two invariants that hold across all four
vertical types — max loss plus max gain equals the strike width, and breakeven
falls between the strikes — checked at a traded price as well as at the midpoint.

Several tests are regressions pinned to bugs unit tests alone could not have
caught: legs drawn from two different expiries; the midpoint sizing breach; the
cost-basis risk total; the per-leg profit target; the unconfirmed cancel; and the
jump-contaminated window that cleared the robustness test by four thousandths.
