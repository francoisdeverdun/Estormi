"""Tests for the deterministic date-fidelity lint."""

from __future__ import annotations

import pytest

from estormi_briefing.lint.fact_lint import (
    allowed_date_set,
    extract_date_mentions,
    extract_deadline_lines,
    lint_dates,
)

pytestmark = pytest.mark.unit


def _flat(text: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for _, c in extract_date_mentions(text):
        out |= c
    return out


# ── extraction ────────────────────────────────────────────────────────────────


def test_extracts_french_english_iso_and_numeric_dates():
    assert _flat("échéance le 22 juin") == {(6, 22)}
    assert _flat("le 1er juin au matin") == {(6, 1)}
    assert _flat("due by Jun 22") == {(6, 22)}
    assert _flat("on 12 Jun at noon") == {(6, 12)}
    assert _flat("réunion 2026-06-17 14:00") == {(6, 17)}
    # Numeric keeps both day/month readings.
    assert _flat("le 16/06") == {(6, 16)}
    assert _flat("on 03/04") == {(4, 3), (3, 4)}


def test_clock_times_and_bare_numbers_not_dates():
    assert _flat("réunion à 9h45 puis à 14:00") == set()
    assert _flat("rappel le 22, environ 75 000 dollars") == set()
    assert _flat("strain 9.4 et HRV 72 ms") == set()
    # "mardi" must not match the "mar" month abbreviation.
    assert _flat("mardi prochain") == set()


# ── allowed set ───────────────────────────────────────────────────────────────


def test_allowed_set_mines_rows_digest_and_day_window():
    rows = {
        "calendar": [{"when": "10:00", "title": "ADR", "date_ts": "2026-06-11T08:00:00Z"}],
        "ctx_rows": [
            {
                "source": "mail",
                "when_label": "2026-03-19 (Thursday)",
                "title": "Firebase",
                "text": "plus de création de workspaces après le 22 juin",
            }
        ],
    }
    allowed = allowed_date_set(rows, news_digest="sommet du 16 juin", date_str="2026-06-11")
    assert (6, 22) in allowed  # mined from row text
    assert (3, 19) in allowed  # mined from when_label
    assert (6, 16) in allowed  # mined from digest
    assert {(6, 11), (6, 12), (6, 13)} <= allowed  # day + 2


# ── lint ──────────────────────────────────────────────────────────────────────


def test_firebase_case_flags_moved_deadline_keeps_real_one():
    rows = {
        "ctx_rows": [
            {
                "source": "mail",
                "when_label": "2026-03-19 (Thursday)",
                "text": "Firebase Studio : plus de nouveaux workspaces après le 22 juin",
            }
        ]
    }
    allowed = allowed_date_set(rows, date_str="2026-06-11")
    draft_ok = "L'échéance Firebase du 22 juin approche. [src: mail · 19 Mar]"
    draft_bad = "La migration Runner est à suivre pour le 16 juin. [src: mail · 19 Mar]"
    assert lint_dates(draft_ok, allowed) == []
    issues = lint_dates(draft_bad, allowed)
    assert len(issues) == 1
    assert issues[0]["type"] == "date_not_in_data"
    assert "16 juin" in issues[0]["excerpt"]


def test_lint_accepts_any_reading_of_ambiguous_numeric():
    allowed = {(6, 3)}  # data has June 3rd
    # Draft writes 03/06 — day/month reading matches → no flag.
    assert lint_dates("rendez-vous le 03/06", allowed) == []


def test_lint_caps_and_dedups_issues():
    allowed = {(6, 11)}
    # The same phantom date twice must yield ONE issue…
    dup = lint_dates("le 1 janvier ici\npuis encore le 1 janvier là", allowed)
    assert len(dup) == 1
    # …and distinct phantom dates are capped at three.
    many = lint_dates("le 1 janvier, le 2 février, le 3 mars, le 4 avril", allowed)
    assert len(many) == 3


def test_lint_refuses_to_guess_when_no_allowed_dates():
    assert lint_dates("le 22 juin", set()) == []


# ── deadline mining ───────────────────────────────────────────────────────────


def test_extract_deadline_lines_requires_keyword_and_date():
    rows = {
        "ctx_rows": [
            {
                "source": "mail",
                "when_label": "2026-03-19 (Thursday)",
                "text": (
                    "Bonjour. Firebase Studio évolue. Plus de création de nouveaux "
                    "workspaces après la date limite du 22 juin. Merci de votre attention."
                ),
            },
            {
                "source": "notes",
                "when_label": "2026-06-01 (Monday)",
                "text": "Une note sans échéance ni date précise, juste du contexte.",
            },
        ]
    }
    lines = extract_deadline_lines(rows)
    assert len(lines) == 1
    assert "22 juin" in lines[0]
    assert "[mail · 2026-03-19 (Thursday)]" in lines[0]


def test_extract_deadline_lines_caps_at_six():
    rows = [
        {
            "source": "mail",
            "when_label": f"2026-06-{d:02d}",
            "text": f"Échéance numéro {d} : à rendre avant le {d} juin sans faute.",
        }
        for d in range(1, 12)
    ]
    assert len(extract_deadline_lines(rows)) == 6


# ── weekday/date mismatch ─────────────────────────────────────────────────────


def test_weekday_mismatch_flagged():
    from estormi_briefing.lint.fact_lint import lint_weekdays

    # 2026-06-16 is a Tuesday — "lundi 16 juin" is the model lying.
    issues = lint_weekdays("Le match France-Sénégal lundi 16 juin à 21h.", "2026-06-11")
    assert len(issues) == 1
    assert issues[0]["type"] == "weekday_date_mismatch"
    assert "lundi 16 juin" in issues[0]["excerpt"]


def test_weekday_match_not_flagged():
    from estormi_briefing.lint.fact_lint import lint_weekdays

    # 2026-06-13 IS a Saturday; 2026-06-12 IS a Friday (English form too).
    text = "Canapé samedi 13 juin à midi, quiz Friday 12 Jun at 13:30."
    assert lint_weekdays(text, "2026-06-11") == []


def test_weekday_next_year_reading_tolerated():
    from estormi_briefing.lint.fact_lint import lint_weekdays

    # 2026-01-15 is a Thursday, but 2027-01-15 is a Friday — a January date
    # written near year-end may mean next year, so "vendredi 15 janvier"
    # must not flag from a 2026 briefing.
    assert lint_weekdays("le vendredi 15 janvier", "2026-12-20") == []


# ── unit-number fidelity ──────────────────────────────────────────────────────


def test_numbers_not_in_source_flags_phantom_figures():
    from estormi_briefing.lint.fact_lint import numbers_not_in_source

    source = "Le bitcoin a décroché sous les 75 000 dollars ; Saylor évoque un rachat à 60 000."
    assert numbers_not_in_source("le cours est passé sous 75 000 $", source) == []
    # 60 000 exists in the source (as the buyback level) — digits present → pass.
    assert numbers_not_in_source("rachat évoqué à 60 000 $", source) == []
    # 50 000 exists nowhere → provable invention.
    bad = numbers_not_in_source("le cours chute sous 50 000 $", source)
    assert bad == ["50000 ($)"]


def test_numbers_ignore_years_dates_and_citations():
    from estormi_briefing.lint.fact_lint import numbers_not_in_source

    source = "annonce faite cette semaine."
    claim = "- Une faille corrigée en 2022 inquiète. (hasheurlive, 2026-06-11)"
    # Years and the bullet's own citation date never count as figures.
    assert numbers_not_in_source(claim, source) == []


def test_numbers_with_units_and_percent():
    from estormi_briefing.lint.fact_lint import numbers_not_in_source

    source = "l'inflation atteint 4,2 % en mai ; récupération à 66%"
    assert numbers_not_in_source("inflation à 4,2 %", source) == []
    assert numbers_not_in_source("récupération à 61%", source) == ["61 (%)"]


def test_french_nbsp_thousands_figures_are_detected():
    """French thousands use a non-breaking space (U+00A0) or narrow no-break
    space (U+202F), not ASCII. The fact gate must still see those figures — the
    separator class previously held only ASCII spaces, so "12 500 €" slipped
    through unchecked."""
    from estormi_briefing.lint.fact_lint import numbers_not_in_source

    # Phantom NBSP / NNBSP thousands figures are now caught (non-empty result).
    assert numbers_not_in_source("le déficit atteint 12 500 €", "rien de chiffré")
    assert numbers_not_in_source("une perte de 1 200 €", "rien de chiffré")
    # The same figure present in source (any space variant) is accepted.
    assert numbers_not_in_source("budget de 12 500 €", "monte à 12 500 € pile") == []


def test_fractions_and_scores_are_not_dates():
    from estormi_briefing.lint.fact_lint import lint_dates

    # "2/3 du budget" must not be read as a date — no false date_not_in_data.
    assert lint_dates("Le conseil a voté 2/3 du budget", {(6, 11)}) == []
    # But a keyword-anchored or disambiguated slash date still counts.
    assert lint_dates("rendez-vous le 16/06", {(6, 11)}) != []
    assert lint_dates("due 16/06/2026", {(6, 11)}) != []


def test_adjacent_source_numbers_not_glued():
    from estormi_briefing.lint.fact_lint import numbers_not_in_source

    source = "Les taux passent à 66, 75 selon la banque"
    assert numbers_not_in_source("Le taux atteint 75 %", source) == []
    assert numbers_not_in_source("Le taux atteint 66 %", source) == []


def test_nearest_future_date_prefers_the_actionable_deadline():
    from datetime import date

    from estormi_briefing.lint.fact_lint import nearest_future_date

    today = date(2026, 6, 12)
    text = (
        "Migrate Firebase Studio projects by Mar 22, 2027. "
        "June 22, 2026: Final date to create new workspaces."
    )
    # The nearest future date wins over the headline 2027 shutdown.
    assert nearest_future_date(text, today) == date(2026, 6, 22)
    # A year-less mention already past this year resolves to next year.
    assert nearest_future_date("réunion du 3 janvier", today) == date(2027, 1, 3)
    assert nearest_future_date("aucune date ici", today) is None
