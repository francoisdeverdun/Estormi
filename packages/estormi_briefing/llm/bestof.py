"""Best-of-N selection through a single-token A/B judge.

A 14B asked to *grade* one text drifts: scores cluster, the rubric gets
re-interpreted call to call, and a confident "8/10" says nothing. The same
model asked "A or B?" between two concrete texts is far more dependable —
the comparison is anchored by both texts sitting in the same prompt. So
quality here is bought with time, not with a smarter model: generate N
candidates, then run a single-elimination tournament where every judge call
answers with exactly one token.

Three properties make the pass safe to bolt onto a run that may take an hour:

- **Bounded**: single elimination costs exactly N-1 judge calls, each
  grammar-locked to one letter (no rambling, no token-budget surprise), and
  :class:`TimeBudget` lets the caller stop generating candidates when the
  wall clock says so.
- **Position-bias resistant**: small models favour one slot; the champion's
  letter alternates duel to duel, so a judge that always answers "A" can
  neither systematically entrench nor dethrone the champion.
- **Never worse than no judge**: an exception, a timeout or a garbage reply
  keeps the current champion and moves on — the tournament degrades to
  "first candidate wins", which is exactly the no-best-of baseline.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger()

# Async LLM callable bound to provider/model by the orchestrator — same
# convention as ``composer.ComposerLlm``: llm(prompt, **decode_opts) -> str.
JudgeLlm = Callable[..., Awaitable[str]]

# The judge's entire vocabulary: a verdict that cannot hedge, explain or
# ramble. One letter in, the answer parses by construction.
AB_GBNF = 'root ::= "A" | "B"'

# max_tokens 3 (not 1): some runtimes count leading-whitespace pieces toward
# the budget; the grammar already caps the *content* at a single letter.
_JUDGE_OPTS = {
    "max_tokens": 3,
    "temperature": 0.0,
    "gbnf_grammar": AB_GBNF,
    "timeout": 90.0,
    "stage": "judge",
}


class TimeBudget:
    """Wall-clock budget for optional quality passes (monotonic clock).

    Best-of-N is a quality pass, not a correctness one — the run may take an
    hour but must not take the whole morning. Callers poll :meth:`exceeded`
    between candidates and stop generating once the budget runs out; whatever
    was already picked still ships. ``minutes <= 0`` means "no budget"
    (never exceeded). Built on ``time.monotonic`` so an NTP step or a DST
    change mid-run cannot silently inflate or zero the budget.
    """

    def __init__(self, minutes: float) -> None:
        self.minutes = float(minutes)
        self._start = time.monotonic()

    def exceeded(self) -> bool:
        """True once elapsed time strictly exceeds the budget."""
        if self.minutes <= 0:
            return False
        return (time.monotonic() - self._start) / 60.0 > self.minutes

    @property
    def remaining_min(self) -> float:
        """Minutes left, floored at 0.0.

        A progress figure, not a verdict: a no-budget instance reports 0.0
        remaining yet never reads as exceeded — gate on :meth:`exceeded`.
        """
        if self.minutes <= 0:
            return 0.0
        return max(0.0, self.minutes - (time.monotonic() - self._start) / 60.0)


def _judge_prompt(a: str, b: str, *, facts: str, criteria: str, language: str) -> str:
    """One duel's prompt — built inline (no template file): the judge is a
    leaf utility and the prompt's structure IS its contract with the parser.

    The facts come first so the judge reads the ground truth before either
    candidate can frame it; the closing instruction is absolute because the
    grammar only enforces the alphabet, not the intent.
    """
    return (
        "You are judging two candidate lines for a morning briefing. The reader "
        f"is the busy user it was written for. Both candidates are in {language}.\n\n"
        "FACTS (ground truth — a candidate contradicting or inventing facts loses):\n"
        f"{facts}\n\n"
        "CRITERIA:\n"
        f"{criteria}\n\n"
        f"A: {a}\n\n"
        f"B: {b}\n\n"
        "Which candidate better satisfies the criteria while staying true to the "
        "FACTS? Answer with exactly one letter: A or B. Nothing else."
    )


async def judge_pick(
    llm: JudgeLlm,
    candidates: list[str],
    *,
    facts: str,
    criteria: str,
    language: str = "French",
    opts: dict | None = None,
) -> str:
    """Single-elimination A/B tournament; returns the winning candidate.

    Empty/whitespace candidates and exact duplicates are dropped first (the
    first occurrence survives) — a duplicate duel spends a judge call to
    learn nothing. No candidate → ``""``; one candidate → returned as-is
    without any judge call.

    The first candidate opens as champion and every later candidate gets ONE
    duel against the current champion. To blunt position bias, the champion's
    letter alternates: it is shown as A on even-indexed duels and as B on
    odd-indexed ones, and the reply is interpreted accordingly — the
    challenger wins only when the judge explicitly votes the challenger's
    letter. Anything else (a vote for the champion, garbage, an empty reply,
    an exception from ``llm``) keeps the champion and the tournament moves to
    the next challenger; this function never raises. The asymmetry is
    deliberate: dethroning requires an unambiguous verdict, so judge failures
    degrade toward the baseline instead of toward noise.
    """
    pool: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if not (c or "").strip() or c in seen:
            continue
        seen.add(c)
        pool.append(c)
    if not pool:
        return ""
    champion = pool[0]
    if len(pool) == 1:
        return champion

    decode = opts if opts is not None else _JUDGE_OPTS
    for duel, challenger in enumerate(pool[1:]):
        champion_is_a = duel % 2 == 0
        a, b = (champion, challenger) if champion_is_a else (challenger, champion)
        try:
            reply = await llm(
                _judge_prompt(a, b, facts=facts, criteria=criteria, language=language),
                **decode,
            )
        except Exception as exc:  # noqa: BLE001 — a dead judge keeps the champion
            log.warning("bestof: judge call failed (%r) — champion kept", exc)
            continue
        verdict = (reply or "").strip().upper()[:1]
        if verdict == ("B" if champion_is_a else "A"):
            champion = challenger
    return champion
