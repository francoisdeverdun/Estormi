"""Tests for the best-of-N A/B judge and its time budget."""

from __future__ import annotations

import time

import pytest

from estormi_briefing.llm.bestof import _JUDGE_OPTS, AB_GBNF, TimeBudget, judge_pick

pytestmark = pytest.mark.unit

_FACTS = "Revue archi à 10:00. Audit cloud à 15:00."
_CRITERIA = "Factual, concise, actionable for the morning."


def _scripted_judge(replies: list):
    """A fake judge that records every call and plays ``replies`` in order.

    A reply that is an Exception instance is raised instead of returned —
    the failure-resilience tests script a dead duel mid-tournament.
    """
    calls: list[dict] = []
    queue = iter(replies)

    async def llm(prompt: str, **kw) -> str:
        calls.append({"prompt": prompt, **kw})
        reply = next(queue)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return llm, calls


# ── pool hygiene ──────────────────────────────────────────────────────────────


async def test_no_usable_candidate_returns_empty_without_judge_call():
    llm, calls = _scripted_judge([])
    assert await judge_pick(llm, [], facts=_FACTS, criteria=_CRITERIA) == ""
    assert await judge_pick(llm, ["", "   ", "\n"], facts=_FACTS, criteria=_CRITERIA) == ""
    assert calls == []


async def test_single_candidate_short_circuits_without_judge_call():
    llm, calls = _scripted_judge([])
    out = await judge_pick(llm, ["seul candidat"], facts=_FACTS, criteria=_CRITERIA)
    assert out == "seul candidat"
    assert calls == []
    # Whitespace siblings don't count as a second candidate either.
    out = await judge_pick(llm, ["  ", "seul candidat", ""], facts=_FACTS, criteria=_CRITERIA)
    assert out == "seul candidat"
    assert calls == []


async def test_exact_duplicates_deduped_keeping_first():
    llm, calls = _scripted_judge(["A"])
    out = await judge_pick(llm, ["x", "x", "y", "x"], facts=_FACTS, criteria=_CRITERIA)
    # One real challenger (y) → exactly one duel, champion x kept by the "A" vote.
    assert out == "x"
    assert len(calls) == 1


# ── verdict parsing ───────────────────────────────────────────────────────────


async def test_b_vote_flips_champion():
    llm, calls = _scripted_judge(["B"])
    out = await judge_pick(llm, ["champ", "chall"], facts=_FACTS, criteria=_CRITERIA)
    assert out == "chall"
    assert len(calls) == 1


async def test_reply_is_stripped_uppercased_first_char():
    # "  b — clearly better" → strip → "b…" → upper → first char "B" → flip.
    llm, _ = _scripted_judge(["  b — clearly better"])
    out = await judge_pick(llm, ["champ", "chall"], facts=_FACTS, criteria=_CRITERIA)
    assert out == "chall"


async def test_garbage_or_empty_reply_keeps_champion():
    for reply in ["A", "", "   ", "Z", "the second one is better"]:
        llm, _ = _scripted_judge([reply])
        out = await judge_pick(llm, ["champ", "chall"], facts=_FACTS, criteria=_CRITERIA)
        assert out == "champ", f"reply {reply!r} should keep the champion"


async def test_exception_keeps_champion_and_tournament_continues():
    # Duel 0 dies; duel 1 is odd-indexed (champion shown as B, challenger as
    # A) and the "A" vote dethrones the champion in favour of z.
    llm, calls = _scripted_judge([RuntimeError("llama down"), "A"])
    out = await judge_pick(llm, ["x", "y", "z"], facts=_FACTS, criteria=_CRITERIA)
    assert out == "z"
    assert len(calls) == 2  # the failure did not abort the tournament


# ── position alternation ──────────────────────────────────────────────────────


async def test_alternation_swaps_which_text_is_labeled_a():
    # Champion always survives ("A" then "B" votes track the champion's
    # letter), so both duels face the ORIGINAL champion x.
    llm, calls = _scripted_judge(["A", "B"])
    out = await judge_pick(llm, ["x", "y", "z"], facts=_FACTS, criteria=_CRITERIA)
    assert out == "x"
    # Duel 0 (even): champion is A, challenger is B.
    assert "A: x" in calls[0]["prompt"] and "B: y" in calls[0]["prompt"]
    # Duel 1 (odd): letters swap — challenger is A, champion is B.
    assert "A: z" in calls[1]["prompt"] and "B: x" in calls[1]["prompt"]


async def test_on_odd_duel_champion_letter_b_is_interpreted_accordingly():
    # Duel 1 answers "B" — under alternation that names the CHAMPION, so the
    # vote must keep x, not crown z.
    llm, _ = _scripted_judge(["A", "B"])
    assert await judge_pick(llm, ["x", "y", "z"], facts=_FACTS, criteria=_CRITERIA) == "x"
    # And "A" on that same odd duel names the challenger → z wins.
    llm, _ = _scripted_judge(["A", "A"])
    assert await judge_pick(llm, ["x", "y", "z"], facts=_FACTS, criteria=_CRITERIA) == "z"


# ── prompt contract & decode opts ─────────────────────────────────────────────


async def test_prompt_carries_facts_criteria_language_and_instruction():
    llm, calls = _scripted_judge(["A"])
    await judge_pick(llm, ["champ", "chall"], facts=_FACTS, criteria=_CRITERIA, language="Italian")
    prompt = calls[0]["prompt"]
    assert _FACTS in prompt
    assert _CRITERIA in prompt
    assert "Italian" in prompt
    assert "morning briefing" in prompt
    assert "A or B" in prompt


async def test_default_opts_are_grammar_locked_and_custom_opts_replace_them():
    llm, calls = _scripted_judge(["A"])
    await judge_pick(llm, ["champ", "chall"], facts=_FACTS, criteria=_CRITERIA)
    kw = {k: v for k, v in calls[0].items() if k != "prompt"}
    assert kw == _JUDGE_OPTS
    assert kw["gbnf_grammar"] == AB_GBNF and kw["temperature"] == 0.0
    llm, calls = _scripted_judge(["A"])
    await judge_pick(
        llm, ["champ", "chall"], facts=_FACTS, criteria=_CRITERIA, opts={"max_tokens": 1}
    )
    assert {k: v for k, v in calls[0].items() if k != "prompt"} == {"max_tokens": 1}


# ── TimeBudget ────────────────────────────────────────────────────────────────


def test_time_budget_zero_or_negative_means_no_budget(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    for minutes in (0.0, -5.0):
        budget = TimeBudget(minutes)
        now["t"] += 86_400.0  # a full day elapses
        assert budget.exceeded() is False
        assert budget.remaining_min == 0.0


def test_time_budget_positive_strict_threshold_and_floor(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    budget = TimeBudget(10.0)
    assert budget.exceeded() is False
    assert budget.remaining_min == 10.0
    now["t"] += 300.0  # 5 min in
    assert budget.exceeded() is False
    assert budget.remaining_min == 5.0
    now["t"] += 300.0  # exactly 10 min — "exceeded" is strictly greater than
    assert budget.exceeded() is False
    assert budget.remaining_min == 0.0
    now["t"] += 0.1
    assert budget.exceeded() is True
    assert budget.remaining_min == 0.0  # floored, never negative
