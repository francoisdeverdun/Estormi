"""Tests for the deterministic day-to-day continuity callbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from estormi_briefing.compose.continuity import (
    STATE_FILE,
    build_state,
    callbacks,
    load_state,
    save_state,
)

pytestmark = pytest.mark.unit

# ── state persistence ─────────────────────────────────────────────────────────


def test_load_missing_file_returns_empty(tmp_path):
    assert load_state(tmp_path) == {}


def test_load_corrupt_or_wrong_shape_returns_empty(tmp_path):
    (tmp_path / STATE_FILE).write_text("{not json at all")
    assert load_state(tmp_path) == {}
    (tmp_path / STATE_FILE).write_text('["a", "list", "not", "a", "dict"]')
    assert load_state(tmp_path) == {}


def test_save_then_load_roundtrip_keeps_unicode(tmp_path):
    state = build_state("2026-06-11", ["Revue ADR — préparation"], "Lède accentué.")
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    # ensure_ascii=False: accents are stored as-is, not \uXXXX escapes.
    assert "préparation" in (tmp_path / STATE_FILE).read_text()


def test_save_creates_missing_nested_dirs(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    save_state(nested, {"date": "2026-06-11", "threads": [], "lede": ""})
    assert json.loads((nested / STATE_FILE).read_text())["date"] == "2026-06-11"


def test_save_failure_never_raises(tmp_path, monkeypatch):
    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    save_state(tmp_path, {"date": "2026-06-11"})  # must not propagate


# ── build_state ───────────────────────────────────────────────────────────────


def test_build_state_cleans_dedupes_and_caps():
    titles = ["  Revue ADR  ", "", "   ", "revue adr", "Budget Q3"]
    titles += [f"Topic {i}" for i in range(10)]
    state = build_state("2026-06-11T07:00:00", titles, "x" * 500)
    assert state["date"] == "2026-06-11"
    assert state["threads"][:2] == ["Revue ADR", "Budget Q3"]  # first spelling wins
    assert len(state["threads"]) == 8
    assert len(state["lede"]) == 300


# ── callbacks ─────────────────────────────────────────────────────────────────

_STATE = {
    "date": "2026-06-10",
    "threads": ["Platform architecture review prep", "Budget sync"],
    "lede": "Tomorrow pivots on the architecture review.",
}


def test_yesterday_match_produces_timed_line_with_calendar_title():
    cal = [{"title": "Platform architecture review (room 4B)", "when": "15:00"}]
    lines = callbacks(_STATE, "2026-06-11", cal, "en")
    assert lines == [
        "↩ Yesterday's briefing was preparing “Platform architecture review (room 4B)”"
        " — that's today at 15:00."
    ]


def test_all_day_and_empty_when_drop_the_time_part():
    cal = [{"title": "Budget sync", "when": "All day"}]
    assert callbacks(_STATE, "2026-06-11", cal, "en") == [
        "↩ Yesterday's briefing was preparing “Budget sync” — that's today."
    ]
    cal = [{"title": "Budget sync", "when": ""}]
    assert callbacks(_STATE, "2026-06-11", cal, "fr") == [
        "↩ Hier, le briefing préparait « Budget sync » — c'est aujourd'hui."
    ]


def test_french_line_format():
    cal = [{"title": "Budget sync", "when": "09:30"}]
    assert callbacks(_STATE, "2026-06-11", cal, "fr") == [
        "↩ Hier, le briefing préparait « Budget sync » — c'est aujourd'hui à 09:30."
    ]


def test_only_exactly_yesterday_speaks():
    cal = [{"title": "Budget sync", "when": "09:30"}]
    assert callbacks(_STATE, "2026-06-12", cal, "en") == []  # two days old
    assert callbacks(_STATE, "2026-06-10", cal, "en") == []  # same day
    assert callbacks(_STATE, "2026-06-09", cal, "en") == []  # state in the future
    # Month boundary is date math, not string math.
    state = dict(_STATE, date="2026-05-31")
    assert callbacks(state, "2026-06-01", cal, "en") != []


def test_garbage_state_or_date_is_silent():
    cal = [{"title": "Budget sync", "when": "09:30"}]
    assert callbacks({}, "2026-06-11", cal, "en") == []
    assert (
        callbacks({"date": "not-a-date", "threads": ["Budget sync"]}, "2026-06-11", cal, "en") == []
    )
    assert callbacks(_STATE, "garbage", cal, "en") == []


def test_prefix_match_survives_different_suffixes_but_not_different_topics():
    # Shared 24-char normalised prefix ("platform architecture re…") matches…
    cal = [
        {"title": "Platform architecture review — salle 4B", "when": "10:00"},
        {"title": "Quarterly planning kickoff", "when": "11:00"},  # …never prepared
    ]
    lines = callbacks(_STATE, "2026-06-11", cal, "en")
    assert len(lines) == 1
    assert "salle 4B" in lines[0]  # calendar title is the current truth
    assert "Quarterly" not in lines[0]


def test_dedupes_calendar_titles_and_caps_at_two_in_calendar_order():
    state = dict(_STATE, threads=["Alpha standup meeting weekly", "Budget sync", "Gamma review"])
    cal = [
        {"title": "Budget sync", "when": "09:00"},
        {"title": "Budget sync", "when": "16:00"},  # duplicate title: one line only
        {"title": "Alpha standup meeting weekly", "when": "10:00"},
        {"title": "Gamma review", "when": "11:00"},  # third match: over the cap
    ]
    lines = callbacks(state, "2026-06-11", cal, "en")
    assert len(lines) == 2
    assert "Budget sync" in lines[0] and "09:00" in lines[0]
    assert "Alpha standup" in lines[1]
