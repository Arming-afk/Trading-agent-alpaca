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

Standing aside is the common case — on 2026-09-01, five of eight symbols were
inside the band, one was refused for a contaminated reading, and two traded.
That is the intended shape, not a quiet day.

Direction only decides *which side* to sell on. `vol.trend_bias` is a
spot-versus-moving-average read whose entire job is to avoid selling puts into a
sustained decline. It is documented as carrying no claim of edge, because the
predecessor to this project spent eight pre-registered experiments failing to
find one in these same names.

**What would falsify this:** if the credit spreads lose money while IV/RV was
above the threshold at entry, the premium was not actually rich — the
thresholds, not the direction calls, were wrong. `scripts/outcomes.py` computes
that from the record; see [What came of it](#what-came-of-it).

### The ratio has a denominator, and the denominator can break

Close-to-close realized volatility cannot tell a jump apart from volatility.
One earnings gap inside a 20-day window inflates the denominator, the ratio
reads low, and options look cheap when what actually happened is that the
yardstick broke — while implied has already been crushed by the same event the
gap came from. The two errors point the same way, which is why the trap is
worth naming: **the agent is most likely to buy premium exactly when premium is
least worth buying.**

It is not hypothetical. On 2026-08-31 NVDA came back at IV/RV 0.63 off a 45%
realized reading and the agent bought a debit spread on it.

So the same window is measured twice — once as-is, once with its single largest
return removed — and a stance has to clear both:

- **contamination:** if more than 20% of the realized reading rests on one
  session, the ratio is not measuring the surface and no stance is taken;
- **robustness:** if dropping that day moves the symbol into a different
  bucket, the signal was that one day.

Both readings go into the log whether or not they changed the decision. The
check is a veto on an existing signal and can only ever remove a trade.

`agent/earnings.py` is the other half: a known print inside the holding period
blocks the trade outright. It ships **empty on purpose** — there is no earnings
endpoint in the Alpaca CLI, and a file of plausible-looking guessed dates would
authorise trades on numbers nobody checked. A symbol with no entry is reported
as `unknown`, never as clear, and the log says which it was.

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
*before* the order is sent. That is what makes the portfolio gates meaningful —
they are arithmetic on a known number, not an estimate from a model. Both legs
ride on one `mleg` order so they fill together or not at all: a partial fill on a
vertical is not a defined-risk position.

**3 · Risk limits are a-priori and are not tuned on the competition.**
Five trading days cannot distinguish a good threshold from a lucky one, and
tuning a limit against the record it is judged by is circular. The one threshold
correction made during the build is documented in `agent/strategy.py` with its
reasoning and its timestamp — it was made before any order was placed, on the
basis of published behaviour rather than results.

The one limit *added* mid-competition is `underlying_risk`, on 2026-09-02, and
the distinction is worth being precise about. It is not a threshold moved to
improve a number: it closes a hole through which a limit already stated here —
that no one spread dominates the book — could be walked around by entering the
same spread twice. Its value is derived from the per-trade cap already in the
table, not from the position that exposed it. But it was chosen by someone who
could see that position, and `agent/config.py` says so where the number lives.

**4 · The record has to survive contact with the account.**
A decision log is not an audit trail if the broker can disagree with it. Alpaca
reports option positions leg by leg and knows nothing about the spread they
belong to, so [`agent/positions.py`](agent/positions.py) joins the two: the
journal is authoritative about structure and intent, the broker about what is
actually on and how large. Everything downstream — portfolio risk, profit
targets, the outcome report — is computed from that join rather than from either
side alone. It is also what makes a breach visible at all: only the join can
tell a correct position from a tripled one.

---

## Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  .github/workflows/daily-trading.yml   every 30 min, 14–19 UTC   │
   │  first pass surveys · the rest manage and chase · Mon–Fri        │
   └────────────────────────────┬─────────────────────────────────────┘
                                ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  scripts/daily_run.py                                            │
   │  clock → account → reconcile → manage → survey → gate → submit   │
   │                                              → chase             │
   └───┬──────────┬───────────┬───────────┬───────────┬───────────────┘
       ▼          ▼           ▼           ▼           ▼
  ┌─────────┐┌──────────┐┌───────────┐┌──────────┐┌────────────┐
  │ market  ││   vol    ││ strategy  ││ earnings ││ positions  │
  │ join 2  ││ IV vs RV ││  regime → ││  event   ││ legs →     │
  │ endpts  ││ + jump   ││  structure││  block   ││ spreads    │
  └────┬────┘└──────────┘└─────┬─────┘└──────────┘└─────┬──────┘
       │                       ▼                        │
       │                 ┌───────────┐            ┌─────▼──────┐
       │                 │  spreads  │            │    risk    │
       │                 │ max_loss  │───────────▶│  5 gates   │
       │                 │  at limit │            │  + sizing  │
       │                 └─────┬─────┘            └─────┬──────┘
       │                       │                        ▼
       │                       │                  ┌────────────┐
       │                       │                  │  advisor   │
       │                       │                  │ veto only  │
       │                       │                  └─────┬──────┘
       └───────────┬───────────┴────────────────────────┘
                   ▼
        ┌─────────────────────┐      ┌──────────────┐
        │ execution → cli     │─────▶│ journal      │
        │ submit · chase      │      │ audit trail  │
        │ resize · close      │      └──────┬───────┘
        └──────────┬──────────┘             ▼
                   ▼                 ┌──────────────┐
        Alpaca Trading API (paper)   │  outcomes    │
                                     │ result vs IV │
                                     └──────────────┘
```

| Module | Responsibility |
|---|---|
| [`agent/cli.py`](agent/cli.py) | Typed wrapper over the official CLI — command construction, JSON parsing, error envelopes, multi-leg submission. |
| [`agent/market.py`](agent/market.py) | Joins the two chain endpoints (see below) into complete, tradable rows. |
| [`agent/chain.py`](agent/chain.py) | OCC parsing, liquidity gating, strike/delta selection. |
| [`agent/vol.py`](agent/vol.py) | Realized vol, ATM implied vol, the IV/RV ratio, trend bias, jump contamination. |
| [`agent/strategy.py`](agent/strategy.py) | Regime → structure → strikes, with a stated reason for every refusal. |
| [`agent/earnings.py`](agent/earnings.py) | Event-risk calendar. Blocks a print inside the holding period; reports `unknown` rather than clear. |
| [`agent/spreads.py`](agent/spreads.py) | Vertical arithmetic: max loss at the midpoint *and at the price the order is sent at*, sizing. |
| [`agent/risk.py`](agent/risk.py) | Four portfolio gates; an order needs a unanimous pass. |
| [`agent/positions.py`](agent/positions.py) | Reconciles broker legs into the spreads the journal recorded. Open risk, package P&L, excess size. |
| [`agent/execution.py`](agent/execution.py) | Submission, the fill chase, and closing a package in one order. |
| [`agent/advisor.py`](agent/advisor.py) | An LLM that can veto an approved trade and do nothing else. |
| [`agent/outcomes.py`](agent/outcomes.py) | Joins every entry back to its result — the falsification test. |
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
| Order state during a chase | `alpaca order get --order-id …` |
| Withdrawing a resting order | `alpaca order cancel --order-id …` |

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
>
> And one that cost real money: `order get` and `order cancel` take the id as
> **`--order-id`**, not positionally. Passed positionally they return
> `{"error": "--order-id required", "status": 0}` — a *zero* status on a failed
> call, which reads like a warning in a log and is not one. See
> [the execution incident](#3--the-cancel-that-was-not-a-cancel).

---

## Risk gates

Four pure functions stand between a decision and an order.
`risk.evaluate()` runs all of them; one failure blocks the trade and the reason
goes into the log.

| Gate | Default | What it prevents |
|---|---|---|
| `drawdown` | 10% | New positions while equity is far below its peak. |
| `portfolio_risk` | 25% | Twenty 2% trades quietly becoming a 40% bet. |
| `underlying_risk` | 5% | Several approved entries stacking up in one name. |
| `daily_trades` | 3/day | A bug or a news day turning into correlated size. |
| per-trade budget | 2% | Any one spread dominating the book. |

`underlying_risk` closes a gap between the other two, and it was found by the
account rather than by reasoning. The per-trade budget looks at one spread and
the portfolio cap looks at all of them; neither looks at a *name*. So the agent
read AAPL as rich on two consecutive days, opened a spread at the same strikes
each time — both approved on their own terms, each inside 2% — and ended up
with 5.2% of the account on one strike pair and one expiry. Two entries at the
same strikes are not two positions: the broker nets them into one, and so does
a gap down. The limit is 2.5x the per-trade cap, which allows two or three
concurrent positions in a name and refuses a fourth. It blocks new risk and
never closes what is open, for the same reason the drawdown breaker does not.

Five rules the tests enforce:

- **Missing data blocks new risk.** Unlike a monitoring system, where a missing
  datum should mean "do not intervene", here the gate is the only thing sizing
  the position — so unknown equity means no trade, not an unsized one.
- **The per-trade budget is clipped to portfolio headroom**, or the portfolio cap
  would be breachable one trade at a time.
- **Sizing uses the price the order is sent at, not the midpoint.** The limit is
  walked toward the marketable side, and that concession always moves in the
  direction of more risk. Sizing on `max_loss` and submitting at
  `limit_price(aggression)` overspends the budget by exactly the concession.
- **Open risk is the sum of each spread's recorded worst case**, taken from the
  journal and scaled to the quantity the broker reports. It is not the sum of
  the legs' cost bases, which for a credit spread answers a different question
  entirely.
- **Every budget is clipped to the headroom of every cap it shares**, the
  portfolio's and the underlying's alike. A cap that only blocks the trade
  which crosses it is breachable one trade at a time.

Positions are closed at **60% of the package's maximum profit**, or at 5 DTE —
whichever comes first, and always as one `mleg` order. Taking profit early on
short premium is not timidity: the last of the credit takes the longest to
arrive and carries the same tail risk throughout.

---

## The schedule

GitHub's scheduled workflows are best-effort, and the delay is unbounded. The
single `0 14 * * 1-5` cron this project started with fired **once at 19:43** —
seventeen minutes before the close — and on the next session did not fire at
all. A schedule that is allowed to silently not happen is not a schedule.

So the run is attempted every thirty minutes through the session and made
idempotent instead:

- the **first pass that lands** does the full survey and may open positions;
- every pass after it runs in **maintenance mode** — reconciling, managing open
  positions and chasing unfilled orders, opening nothing new.

`journal.completed_full_run_today()` is what tells them apart, and the 3-trades
daily cap is counted from the journal across every pass, so twelve attempts a
day cannot become twelve trades.

[`heartbeat.yml`](.github/workflows/heartbeat.yml) runs at 20:15 UTC and **fails
if the day has no run record at all**. A failed workflow is the notification —
GitHub emails the repository owner without any extra service to configure. It
deliberately does not check P&L or fills: standing aside all day is designed
behaviour and must not page anyone.

---

## Execution

A limit sent once is a bid, not an execution strategy. A two-leg package quoted
a dime wide on each leg will not fill at the midpoint just because the midpoint
is fair.

[`agent/execution.py`](agent/execution.py) submits at a modest concession, waits,
and re-quotes closer to the marketable side if the package has not traded —
three rounds by default, ending at "cross both markets", past which there is
nothing left to concede.

Two rules make that safe rather than merely persistent:

- **Every re-quote is re-sized.** A worse price is more risk. Re-pricing a debit
  spread from $1.31 to $1.38 at the same quantity spends budget no gate granted,
  so the quantity is recomputed at the new price and the position shrinks if it
  has to. A chase that cannot fit the budget is abandoned, not forced.
- **A replacement is never sent until the original is known to be gone.** A
  failed cancel followed by a re-submission is not a re-quote; it is a second
  live order for the same intent. If a cancel cannot be confirmed, the chase
  stops.

An order whose status cannot be read is left resting rather than cancelled: a
resting day order either fills at an approved price or expires, while cancelling
something in an unknown state and replacing it is how one intent becomes two.

---

## The advisor

The only model-in-the-loop component, and the shape of its authority is the
whole design:

> The advisor can **veto** a trade the deterministic path already approved.
> It cannot propose one. It cannot choose a strike, an expiry, or a size.
> It cannot overturn a risk gate, in either direction.

Everything this project claims — defined risk before submission, a-priori
thresholds, a log that explains the position — rests on the decision path being
reproducible from its inputs. A model that could size a position would take that
away, and no five-day track record could win it back. A model that can only
subtract trades leaves every guarantee intact: the worst case is that the agent
trades less than it otherwise would.

It runs last, after all four gates have passed, and answers one question: *is
there a reason not to do this that the rules did not encode?* That is what the
rules are worst at, because the rules only see a volatility surface and an
account balance. The brief it receives deliberately excludes equity, buying
power and quantity — knowing the account invites reasoning about size, and size
is not its decision.

It **fails open**. A timeout, a bad key, an unparseable reply, no key at all —
all return "no objection" and the deterministic decision stands. Failing closed
would hand an API outage the power to halt the strategy, which is a larger risk
than the one the advisor removes. A veto also has to be *legible*: affirmative,
with a stated reason. And a run in which the advisor vetoes more than 75% of
candidates treats the advisor as faulty rather than as right.

Off by default (`LLM_ENABLED=0`). An agent whose behaviour depends on whether an
API key happened to be present is not reproducible.

---

## The audit trail

`logs/decisions.jsonl` records **every symbol considered, not only the traded
ones.** A log that keeps two trades and drops six refusals reads as a strategy
that fired twice, when it actually declined six times for stated reasons — and
the refusals are the larger part of this strategy's behaviour. Each record
carries the IV/RV reading (both of them), the structure, the risk verdict, the
advisor's opinion, and the argv a human could re-run.

```
SPY    declined  IV/RV=1.485  ex-jump 1.581  →  bear_call_credit 789/781
MSFT   declined  IV/RV=0.965  ex-jump 1.110  inside [0.85, 1.30] — no edge claimed
NVDA   declined  IV/RV=0.635  ex-jump 0.846  rests on one session — 25% of
                              realized vol is a single day
```

## What came of it

```bash
python3 scripts/outcomes.py            # joins the broker's marks
python3 scripts/outcomes.py --offline  # journal only, no network
```

One row per submitted spread: the regime reading at entry, and the dollars that
followed from it. Three states are kept distinct on purpose — **open** (marked,
not a result), **closed** (realized), and **unfilled** (the order never traded).
An unfilled order did not break even; it did not happen, and averaging it in as
a zero would describe a strategy that broke even on trades it never made.

The aggregate splits credit from debit and never pools them: the credit side
tests the variance risk premium, the debit side is a directional bet with a
volatility trigger, and pooling lets one hide inside the other. Below 20
resolved trades the report says the sample cannot distinguish the thesis from
noise, in as many words, rather than printing a win rate that would be read as
evidence.

---

## What the live account taught us

Four defects survived 175 unit tests and a dry run, and every one of them needed
a real account to surface. They are documented here rather than quietly fixed,
because the failure modes are more transferable than the strategy.

### 1 · Sizing used a price the order was never sent at

`max_loss` prices the package at the midpoint. The order goes out at
`limit_price(aggression)`, which is always worse. NVDA was sized at 16 contracts
against a $1.25 mid — $2,000, the 2% budget to the dollar — and submitted at
$1.31, which is **$2,096, or 2.10% of equity**. Not a rounding error: a gate
doing exact arithmetic on a number the order was never going to trade at.

*Fix:* `Vertical.max_loss_at(price)`, and `size_for_risk` takes the limit.

### 2 · Open risk was arithmetic on the wrong number

The runner summed `abs(cost_basis)` across legs. For a credit spread that is
unrelated to the worst case — the cost basis is the credit collected, the risk
is the width less that credit. For the live NVDA position it reported **$24,160
of risk against a true $2,000**, which would have consumed the 25% portfolio gate
and quietly stopped the agent trading while looking like caution.

*Fix:* [`agent/positions.py`](agent/positions.py).

### 3 · The cancel that was not a cancel

`alpaca order cancel <id>` is rejected — the id has to arrive as `--order-id` —
and the CLI reports the rejection with `"status": 0`, which reads like a soft
warning. During the first chased run, six cancels failed and the chase sent a
more aggressive replacement after every one. **All of them filled.** The account
ended the run carrying six SPY spreads against an approved two, and twelve AAPL
against four: 4.0% and 5.0% of equity each, against a 2% per-trade cap.

The flag was the trigger. The bug was that a re-quote could proceed on an
unconfirmed cancel at all.

*Fix:* `_cancel` returns whether the order is known to be gone, and `_requote`
will not send a replacement unless it is. `OpenSpread.excess_qty` makes the
resulting breach visible — the broker reports a position and nothing about how
large it was supposed to be, so only the journal join can tell a correct
position from a tripled one. `scripts/ops.py trim` closed exactly the excess and
left the approved position alone; the account was back inside every limit
thirteen minutes later, and the whole sequence is in the log.

### 4 · The profit target was applied per leg

On a credit spread the short leg reaches +60% of its own cost long before the
package reaches 60% of max gain — and closing that leg alone strands the long
one. A defined-risk position turned into a single option by its own exit rule.

*Fix:* closes measure the package and flatten it in one `mleg` order.

### And one the filter caught by four thousandths

The jump filter shipped in the morning. Hours later NVDA came back at IV/RV
0.635 with **24.9% of its realized vol carried by one session**, and the ex-jump
ratio landed at **0.846 against a 0.85 threshold**. The stance did not flip, so
the robustness test had nothing to say, and a reading that was a quarter one
earnings gap was about to be traded as a statement about the volatility surface.
A contaminated window is now refused on its own terms, whether or not the stance
survives it. The ordinary readings from the same run — 6.1% to 18.5% — are all
below the tolerance and untouched, which is the check that matters.

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
writes every log, but submits nothing. Scheduled workflow runs set it to `1`; a
manual dispatch trades only when explicitly asked to.

## Operator commands

```bash
python3 scripts/ops.py report              # positions, reconciled spreads, working orders
python3 scripts/ops.py trim --dry-run      # what is larger than the gate approved
python3 scripts/ops.py trim                # cut it back to the approved size
python3 scripts/ops.py cancel-open-orders  # flatten the order book
```

Manual only, and mirrored by [`ops.yml`](.github/workflows/ops.yml) so it can be
run without local credentials. It cancels orders and trims excess; it never
flattens an approved position, because an open order is an intention that has
not happened yet while a position is a trade.

## Dashboard

```bash
streamlit run dashboard/app.py
```

Shows the equity curve, the latest volatility scan, the full decision table —
every symbol considered, with the reason it was or was not traded — and the
outcome of every submitted spread against the reading that caused it. It reads
the committed logs and, when the CLI is installed, live account state; on a host
without the CLI it degrades to the logs alone rather than failing.

## Watching a live run

```bash
python3 scripts/watch.py --until-settled
```

Polls orders, fills and open spreads. Written for the first real submission: a
dry run proves the request body is right, but only a live book shows whether a
multi-leg limit fills and how far from the mid it has to sit to do it.

## Tests

```bash
python3 -m pytest -q      # 296 tests, no network
```

`subprocess.run` is faked, so the CLI tests assert command construction
(credentials stay in the environment, never argv) and success/failure
discrimination. The spread arithmetic is checked against hand-computed values
plus two invariants that hold across all four vertical types — max loss plus max
gain equals the strike width, and breakeven falls between the strikes — which
catch a sign error in any single formula, and are checked at a traded price as
well as at the midpoint.

Several tests are regressions pinned to bugs the live API exposed and unit tests
could not have: the implied-vol field position, the two-endpoint join, legs drawn
from different expiries, the midpoint sizing breach, the cost-basis risk total,
the per-leg profit target, and the unconfirmed cancel.

## License

MIT — see [LICENSE](LICENSE).
