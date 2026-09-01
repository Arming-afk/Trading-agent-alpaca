"""An LLM that can refuse a trade, and can do nothing else.

This is the only model-in-the-loop component in the agent, and the shape of its
authority is the whole design. It is worth stating plainly, because "AI trading
agent" usually means the opposite:

    The advisor can veto a trade the deterministic path already approved.
    It cannot propose one. It cannot choose a strike, an expiry, or a size.
    It cannot overturn a risk gate, in either direction.

The reason is not caution for its own sake. Everything this project claims —
that risk is defined before submission, that thresholds were set a-priori, that
the decision log explains the position — rests on the decision path being
reproducible from the inputs. A model that could size a position would take
that property away, and there is no five-day track record that could win it
back. A model that can only subtract trades leaves every guarantee intact: the
worst case is that the agent trades less than it otherwise would.

So the advisor sits at the end of the chain, after the regime read, after the
structure is built, after all four risk gates have passed, and answers one
question: *is there a reason not to do this that the rules did not encode?*
That is the question the rules are worst at, because the rules only know the
volatility surface and the account. It knows nothing about a pending merger, a
guidance cut, or a Fed morning.

**It fails open.** A timeout, a bad key, a malformed reply, no key at all — all
of them return "no objection" and the deterministic decision stands. The
alternative, failing closed, hands an outage the power to halt the strategy,
which is a larger risk than the one the advisor removes. Every call and every
verdict is written to the decision log either way, including the ones that
errored, so the record shows what the model saw and what it said.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from agent import config

logger = logging.getLogger(__name__)

#: Hard cap on how much a veto may be trusted. The advisor is a filter on an
#: already-approved trade; a model that vetoes everything is broken, not
#: cautious, so a run that loses more than this fraction of its candidates to
#: the advisor keeps the trades and flags the advisor instead.
MAX_VETO_FRACTION = 0.75

SYSTEM_PROMPT = """\
You review options trades that have already been approved by a deterministic \
rules engine. Your only power is to veto. You cannot resize, restructure, or \
suggest a different trade, and you must not try.

The engine has already verified all of the following, so do not re-derive them \
and do not veto on them:
- the position is a defined-risk two-leg vertical
- its maximum loss is within the per-trade and portfolio risk budgets
- both legs passed a liquidity gate (bid/ask width and open interest)
- the volatility regime reading is outside the no-trade band

Veto ONLY for event or context risk the engine cannot see from a volatility \
surface and an account balance: a scheduled binary event inside the holding \
period, a pending corporate action, an unusual known catalyst, or an internal \
contradiction in the numbers you are shown.

Do NOT veto because you dislike the direction, because you would prefer \
different strikes, because you think volatility might change, or because \
trading is risky. Those are not reasons; they apply to every trade.

Answer with a single JSON object and nothing else:
{"veto": true|false, "reason": "<one sentence, under 25 words>"}
Default to {"veto": false, ...} when you have no specific, nameable concern."""


@dataclass
class Verdict:
    """What the advisor said about one candidate."""
    veto: bool
    reason: str
    #: "ok" when the model answered, otherwise why it did not.
    status: str = "ok"
    model: str = ""
    raw: str = ""

    @property
    def allowed(self) -> bool:
        return not self.veto

    def as_log(self) -> dict:
        return {
            "veto": self.veto,
            "reason": self.reason,
            "status": self.status,
            "model": self.model,
        }


@dataclass
class Advisor:
    """A thin, optional client. Construct once per run."""
    enabled: bool = False
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: int = 20
    #: Every verdict this run produced, for the run record.
    verdicts: list[Verdict] = field(default_factory=list)

    @classmethod
    def from_config(cls) -> "Advisor":
        return cls(
            enabled=bool(config.LLM_ENABLED and config.LLM_API_KEY),
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            timeout=config.LLM_TIMEOUT,
        )

    def _client(self):
        from openai import OpenAI          # imported lazily: optional dependency
        return OpenAI(api_key=self.api_key, base_url=self.base_url,
                      timeout=self.timeout, max_retries=1)

    def review(self, brief: dict) -> Verdict:
        """Ask for an objection to one candidate. Never raises."""
        if not self.enabled:
            verdict = Verdict(False, "advisor disabled", status="disabled")
            self.verdicts.append(verdict)
            return verdict

        try:
            response = self._client().chat.completions.create(
                model=self.model,
                temperature=0,
                max_tokens=160,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(brief, default=str)},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
            verdict = _parse(raw, model=self.model)
        except Exception as exc:                      # noqa: BLE001 — fail open
            logger.warning("advisor unavailable (%s) — deferring to the rules", exc)
            verdict = Verdict(False, f"advisor unavailable: {type(exc).__name__}",
                              status="error", model=self.model)

        self.verdicts.append(verdict)
        return verdict

    def sanity_check(self, considered: int) -> str | None:
        """Guard against an advisor that has become a blanket refusal.

        A model answering "veto" to everything is indistinguishable, from
        inside a single run, from a model that is right about everything — so
        the tie is broken by design rather than by trust: past this fraction
        the advisor is treated as faulty and its vetoes are reported rather
        than obeyed.
        """
        if considered <= 0:
            return None
        vetoed = sum(1 for v in self.verdicts if v.veto)
        if vetoed / considered > MAX_VETO_FRACTION:
            return (f"advisor vetoed {vetoed}/{considered} candidates "
                    f"(> {MAX_VETO_FRACTION:.0%}) — treated as faulty, vetoes ignored")
        return None


def _parse(raw: str, *, model: str = "") -> Verdict:
    """Read the model's reply. Anything unparseable is not a veto.

    A veto has to be affirmative and legible. Silence, prose, a refusal to
    answer, half a JSON object — none of those are an objection, and reading
    them as one would let a formatting failure stop the strategy.
    """
    text = raw.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return Verdict(False, "no parseable verdict", status="unparseable",
                       model=model, raw=text[:400])
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Verdict(False, "no parseable verdict", status="unparseable",
                       model=model, raw=text[:400])

    veto = payload.get("veto")
    if not isinstance(veto, bool):
        veto = str(veto).strip().lower() in ("true", "yes", "1")
    reason = str(payload.get("reason") or "").strip()[:240]

    # A veto with no stated reason is not actionable and is not recorded as one:
    # the log has to be able to say why a trade did not happen.
    if veto and not reason:
        return Verdict(False, "veto with no stated reason — ignored",
                       status="unreasoned", model=model, raw=text[:400])

    return Verdict(veto, reason or "no objection", status="ok", model=model,
                   raw=text[:400])


def brief_for(candidate, *, event_check=None, jump_ratio: float | None = None,
              today=None) -> dict:
    """The facts the advisor is shown. Deliberately small.

    It sees the position and its context, not the account: the model has no
    business knowing the equity, because knowing it invites reasoning about
    size, and size is not its decision.
    """
    spread = candidate.spread
    regime = candidate.regime
    brief = {
        "today": str(today) if today else None,
        "symbol": regime.symbol,
        "structure": spread.kind,
        "expiration": str(spread.long_leg.expiration),
        "days_to_expiry": spread.long_leg.dte(today) if today else None,
        "long_strike": spread.long_leg.strike,
        "short_strike": spread.short_leg.strike,
        "net_price_per_share": round(spread.net_mid, 4),
        "implied_vol": regime.implied_vol,
        "realized_vol_20d": regime.realized_vol,
        "iv_rv_ratio": regime.ratio,
        "trend_bias": regime.bias,
        "rationale": candidate.rationale,
    }
    if jump_ratio is not None:
        brief["realized_vol_carried_by_largest_day"] = round(jump_ratio, 3)
    if event_check is not None:
        brief["earnings_calendar"] = event_check.as_log()
    return brief
