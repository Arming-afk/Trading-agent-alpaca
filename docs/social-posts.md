# Build-in-public drafts

Five posts for the social engagement challenge — up to five links may be
submitted. Each is built on something that actually happened, because the
findings are the part other people can use.

**Tag on X:** @lablabai · @AlpacaHQ ·
**Tag on LinkedIn:** lablab.ai · Alpaca

Repo: <https://github.com/Arming-afk/Trading-agent-alpaca>

---

## 1 · The thesis (post first)

### X

> Building an options agent for the @AlpacaHQ x @lablabai hackathon.
>
> It does not predict direction. My last project spent 8 pre-registered
> experiments looking for a directional edge in large caps and graded every one
> NOT SUPPORTED. Repeating that would be repeating a finished experiment.
>
> So it trades a structural claim instead: options are usually priced above the
> volatility that actually arrives.
>
> Implied ÷ realized ≥ 1.30 → sell a defined-risk credit spread
> ≤ 0.85 → buy a debit spread
> in between → do nothing
>
> Doing nothing is the common case. 6 of 8 symbols, most days.
>
> 🧵 what I learn as I go

### LinkedIn

> **I'm building a trading agent that refuses to predict direction.**
>
> This week I'm building an autonomous options agent for the Alpaca × lablab.ai
> hackathon, and the first design decision was to throw away the obvious one.
>
> My previous project ran eight pre-registered experiments hunting for a
> directional edge in large-cap equities — momentum, cross-sectional ranking,
> volatility signals, fundamentals. Every one graded NOT SUPPORTED under honest
> inference. One of them I had to retract after correcting the methodology.
>
> Building this entry on a directional forecast would be repeating an experiment
> I already know the answer to.
>
> So it trades something structural instead: **options are usually priced above
> the volatility that subsequently arrives.** The variance risk premium is one of
> the better-documented effects in options markets, and unlike a forecast it
> doesn't require being right about where a stock is going.
>
> Each run it measures implied volatility against 20-day realized, per symbol:
>
> → ratio ≥ 1.30, premium is rich, sell a defined-risk credit spread
> → ratio ≤ 0.85, premium is cheap, buy a debit spread
> → anywhere in between, stand aside
>
> That middle band is where it lives most of the time — six of eight symbols on
> a typical scan. A strategy that trades every day isn't disciplined, it's just
> busy.
>
> Everything is open source, including the decision log with every refusal in it.
> More as I go.
>
> #AlgorithmicTrading #OptionsTrading #Python #BuildInPublic

---

## 2 · The API trap (the most useful one to others)

### X

> Spent an hour today on an options agent that rejected every single contract.
>
> 62 of 62 SPY strikes filtered out. Markets as tight as 0.2% wide.
>
> The cause, for anyone building on @AlpacaHQ options:
>
> `data option chain` → quotes, greeks, implied vol. **No open interest.**
> `option contracts` → open interest. **No quotes, no greeks.**
>
> Filter on OI against the chain alone and every contract arrives with an
> implicit zero. Nothing passes. No error, no warning — just an agent that
> stands aside forever and looks like it's working.
>
> Fix: join them on the OCC symbol.

### LinkedIn

> **My trading agent rejected 100% of contracts and told me nothing was wrong.**
>
> Debugging story from the Alpaca hackathon, for anyone who builds against an
> options API.
>
> My agent filters contracts for liquidity before it will trade them — bid/ask
> under 10% of mid, open interest over 100. Reasonable. It rejected 62 of 62 SPY
> strikes, including ones quoted 0.2% wide, which is about as liquid as an option
> gets.
>
> The instinct is to loosen the filter. That would have been exactly wrong.
>
> The cause: **neither Alpaca options endpoint returns everything.**
>
> • `data option chain` gives quotes, greeks, implied volatility — but no open
>   interest.
> • `option contracts` gives open interest — but no quotes and no greeks.
>
> I was filtering on open interest against the chain endpoint, so every contract
> arrived with an implicit zero and failed. Silently. The agent looked like it
> was running fine and finding nothing worth trading.
>
> Two more from the same afternoon:
>
> • `impliedVolatility` is a **sibling** of the `greeks` object, not a field
>   inside it. Read it from inside and you get None for every contract — and a
>   volatility strategy that stands aside forever.
> • The chain endpoint returns whatever fits under `--limit` unless you pass the
>   expiry range explicitly. That's the nearest weeklies, so the liquid monthly
>   expiry is never in the response at all.
>
> None of these throw. They all degrade into an agent that quietly does nothing,
> which is the worst failure mode there is — it looks like caution.
>
> The lesson I keep relearning: **when a filter rejects everything, suspect the
> data before you loosen the filter.**
>
> #SoftwareEngineering #APIs #OptionsTrading #BuildInPublic

---

## 3 · The bug that made the risk math a fiction

### X

> Worst bug of the build, and unit tests could never have caught it.
>
> My agent picks two strikes to build a vertical spread. It picked the short leg
> by delta, then searched for the protective strike separately.
>
> Both searches ran across the whole chain.
>
> One landed on the Sep 9 expiry. The other landed on Sep 8.
>
> That is not a spread. The legs don't offset. Which means `max_loss` — the
> number every single risk gate sizes against — was describing a position that
> didn't exist.
>
> Found it because I ran it against the live API instead of my own fixtures.
> My test chain only had one expiry in it.

### LinkedIn

> **A bug that made every risk limit in my trading agent meaningless.**
>
> My options agent only opens defined-risk spreads — two legs, same expiry,
> different strikes. The appeal is that the worst case is knowable exactly, in
> dollars, before the order is sent. Every portfolio limit I have is arithmetic
> on that one number.
>
> Which means if that number is wrong, every limit is wrong.
>
> The agent picked the short leg by delta, then searched separately for the
> protective strike. Both searches ran over the full chain. A liquid underlying
> lists several expiries inside any reasonable window — so one search landed on
> September 9 and the other on September 8.
>
> Two options in different weeks are not a vertical spread. They don't offset.
> The "max loss" my risk gates were sizing against described a position that did
> not exist.
>
> My unit tests all passed. They had always passed. My test fixture generated a
> chain with **one** expiry in it, so the bug was structurally invisible to every
> test I had written.
>
> It surfaced within minutes of pointing the thing at the real API.
>
> Two things I'm taking from it:
>
> 1. Test fixtures encode your assumptions. Mine assumed a chain has one expiry,
>    so my tests could only ever confirm what I already believed.
> 2. There is a category of bug that only exists in the shape of real data. Some
>    integration contact is not optional, however good the unit tests look.
>
> The regression test is in the repo now — and it fails against the old code,
> which is the only kind of regression test worth having.
>
> #SoftwareEngineering #Testing #FinTech #BuildInPublic

---

## 4 · Catching my own bad calibration

### X

> Caught myself doing the thing I explicitly wrote a rule against.
>
> My agent sells options when they're "expensively priced" — implied vol high
> relative to realized. I set the cheap threshold at 0.95.
>
> Then a dry run traded 6 of 8 symbols and I looked at why.
>
> The variance risk premium is normally *positive*. A typical implied/realized
> ratio sits above 1.0. So 0.95 isn't "unusually cheap" — it's the middle of the
> normal range.
>
> I'd written a rule that fires on the ordinary case and called it a signal.
>
> Fixed to 0.85/1.30 before placing a single order. Now 6 of 8 stand aside.
>
> The docstring said "standing aside is the common case." The code disagreed.
> The code was wrong.

### LinkedIn

> **My trading agent's docstring and its behaviour disagreed. The docstring was
> right.**
>
> I wrote, in the strategy module: *"Standing aside is the most common outcome by
> design."*
>
> Then I ran it. Six of eight symbols traded. Five of them bought options on the
> claim that premium was unusually cheap.
>
> The thresholds were mine and I'd set them casually: sell when implied ÷
> realized volatility ≥ 1.25, buy when ≤ 0.95, otherwise do nothing.
>
> The problem is that the variance risk premium is **normally positive** —
> implied has historically run a few volatility points above what subsequently
> arrives. So a typical reading sits above 1.0, not at it. Setting "cheap" at
> 0.95 puts the trigger in the middle of the ordinary range.
>
> I hadn't built a signal. I'd built a rule that fires on the normal case and
> called it a dislocation.
>
> Corrected to 0.85 / 1.30 — outside the ordinary band on both sides. Six of
> eight now stand aside, and only genuinely unusual readings trade.
>
> The part I want to be precise about: **this correction was made before a single
> order was placed, and it's justified by published behaviour of the effect, not
> by results.** That distinction matters. Adjusting a threshold because this
> week's P&L looked bad would be fitting the strategy to the record it's being
> judged on. The commit message says which one this was, and the code carries a
> note that these are not to be revisited on the strength of the competition's
> numbers.
>
> Easiest way to fool yourself in quantitative work is to tune a parameter and
> call it a finding.
>
> #QuantitativeFinance #AlgorithmicTrading #BuildInPublic

---

## 5 · Results (post at the end — fill in the actuals)

### X

> Final day of the @AlpacaHQ x @lablabai hackathon. My options agent traded
> itself all week on a paper account.
>
> Result: [P&L]
> Trades: [N] · Stood aside: [N] symbol-days
>
> What I'd defend regardless of the number: it never took an undefined-risk
> position, never breached a portfolio limit, and logged a reason for every
> single refusal — not just the trades.
>
> Five days of P&L is noise. The record is the deliverable.
>
> [repo link]

### LinkedIn

> **Five days, one autonomous options agent, and what I'd say about the result
> either way.**
>
> My agent for the Alpaca × lablab.ai hackathon ran unattended all week on a
> $100,000 paper account — reading the volatility surface each morning, opening
> defined-risk spreads through Alpaca's CLI, managing its own exits.
>
> Result: [fill in]
> Positions opened: [N]. Symbol-days it declined to trade: [N].
>
> Here's the thing I want to say before quoting any number: **five trading days
> of P&L is noise.** It's noise for me and it's noise for everyone else in the
> competition. A good result this week would not prove the strategy works, and a
> bad one wouldn't prove it doesn't.
>
> So what would I actually defend?
>
> → Every position was defined-risk. The worst case was known in dollars before
>   the order went out, never estimated afterwards.
> → No portfolio limit was breached, because the limits are arithmetic on that
>   known number rather than on a model's guess.
> → Every symbol it considered is in the log with a stated reason — including
>   the refusals, which outnumber the trades several times over. A log that keeps
>   only the trades tells you what a strategy did on its best days.
> → The thresholds were fixed before the first order and never touched again.
>
> Four real bugs surfaced during the build, every one of them found by running
> against the live API rather than my own fixtures. I've written them all up.
>
> Code, decision log and the full write-up: [repo link]
>
> #AlgorithmicTrading #OptionsTrading #BuildInPublic #Python
