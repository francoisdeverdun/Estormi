"""Unit tests for estormi_briefing/lint/narration_lint.py.

The spoken-edition rewrite is best-effort; :func:`narration_regressed` is the
conservative guard that flags a stub rewrite so the caller reads the verbatim
body instead. These lock the four gross-loss triggers and — importantly — that
a faithful rewrite (shorter, times/percentages respelled for the ear) does NOT
trip the guard.
"""

from __future__ import annotations

import pytest

from estormi_briefing.lint.narration_lint import narration_regressed

pytestmark = pytest.mark.unit

_TITLE = "Ma journée du lundi"
_BODY = (
    "Ma journée. Réunion à 9 h avec l'équipe produit, puis point à 14:00. "
    "Le climat se réchauffe, selon Le Monde. À noter côté marchés. "
) * 6
# A faithful spoken edition: title kept, times respelled aloud, source woven in.
_GOOD = (
    "Ma journée du lundi. Ta journée s'ouvre à neuf heures avec l'équipe "
    "produit, puis un point à quatorze heures. Côté actualité, le climat se "
    "réchauffe, d'après Le Monde. "
) * 6


def test_faithful_rewrite_is_not_flagged() -> None:
    # Shorter than the body and with times spelled for the ear — but complete.
    assert narration_regressed(_BODY, _GOOD, _TITLE) is False


def test_empty_narration_flags_when_there_was_content() -> None:
    assert narration_regressed(_BODY, "", _TITLE) is True
    assert narration_regressed(_BODY, "   ", _TITLE) is True


def test_empty_body_never_flags() -> None:
    # Nothing to lose → never a regression, whatever the narration is.
    assert narration_regressed("", "", "") is False
    assert narration_regressed("", "Bonjour.", "") is False


def test_stub_rewrite_flags_on_word_ratio() -> None:
    stub = "Ma journée du lundi. Bonjour, voici ton point du jour à neuf heures."
    assert narration_regressed(_BODY, stub, _TITLE) is True


def test_dropped_title_flags() -> None:
    # None of the title's distinctive tokens survive → regression, even though
    # the narration is otherwise long, timed and sourced.
    assert narration_regressed(_BODY, _GOOD, "Zephyrion Kastellan") is True


def test_dropped_all_times_flags() -> None:
    # Body carries clock times; a long narration with no time (neither digit nor
    # spoken form) has lost the schedule.
    timeless = (
        "Ma journée du lundi. Tu vois l'équipe produit, puis un point plus tard. "
        "Côté actualité, le climat se réchauffe, d'après Le Monde. "
    ) * 6
    assert narration_regressed(_BODY, timeless, _TITLE) is True


def test_dropped_all_sources_flags() -> None:
    sourceless = (
        "Ma journée du lundi. Ta journée s'ouvre à neuf heures avec l'équipe "
        "produit, puis un point à quatorze heures. Le climat se réchauffe. "
    ) * 6
    assert narration_regressed(_BODY, sourceless, _TITLE) is True


def test_no_title_argument_skips_the_title_check() -> None:
    # Called without a title, the title clause must be a no-op (not a false flag).
    assert narration_regressed(_BODY, _GOOD) is False
