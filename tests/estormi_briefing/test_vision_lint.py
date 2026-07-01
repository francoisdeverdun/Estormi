"""Tests for the deterministic day-vision structure lint."""

from __future__ import annotations

import pytest

from estormi_briefing.lint.vision_lint import lint_vision, stage_issues

pytestmark = pytest.mark.unit

_GOOD_FR = """READINESS: Récupération correcte, garde le créneau du midi léger.

OBJECTIVE: La journée se joue sur la plateforme data, de l'ADR du matin à la revue du soir.

La colonne vertébrale du jour, c'est la plateforme data. Deux heures d'ADR dès 10h,
puis la seconde session de la revue avec le partenaire de 15h à 17h : les conclusions
de l'une nourrissent l'autre, et tout débouche sur le point leadership à 17h.
Prépare ce que tu veux faire remonter, car la revue se termine à 17h pile et la
réunion s'enchaîne sans pause. Entre les deux blocs techniques, tu reçois une
candidate en entretien à 14h ; son profil est joint à l'invitation, à relire avant.
Côté maison, deux choses à régler : lancer une machine, et réserver les voitures
pour le mariage d'octobre — autant traiter les locations d'un coup pendant une pause.

AROUND: Quelques sujets gravitent autour de la journée sans rien exiger aujourd'hui.
- Demain s'annonce chargé : daily, rétro, et le quiz cybersécurité à 13h30. [src: agenda · 12 juin]
- Commander les courses, attendu pour demain. [src: reminder · 12 juin]
"""


def test_clean_briefing_yields_no_issues():
    assert lint_vision(_GOOD_FR, language="French") == []


def test_missing_objective_flagged():
    text = "Une journée dense.\n\nAROUND: rien de plus."
    types = {i["type"] for i in lint_vision(text)}
    assert "missing_objective" in types


def test_missing_around_flagged():
    text = "OBJECTIVE: une seule ligne.\n\nDu texte de journée."
    types = {i["type"] for i in lint_vision(text)}
    assert "missing_around" in types


def test_bullet_in_my_day_flagged():
    text = (
        "OBJECTIVE: la journée.\n\n"
        "Un paragraphe correct pour commencer la journée et donner du contexte.\n"
        "- un bullet interdit ici\n\n"
        "AROUND: rien.\n"
    )
    types = {i["type"] for i in lint_vision(text)}
    assert "bullet_in_my_day" in types


def test_unsourced_around_bullet_flagged():
    text = _GOOD_FR + "\n- un item sans attribution\n"
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "unsourced_around_bullet" in types


def test_french_label_variant_flagged():
    text = _GOOD_FR.replace("OBJECTIVE:", "OBJECTIF :")
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "label_not_english" in types
    assert "missing_objective" in types


def test_english_drift_flagged_in_french_run():
    text = (
        "OBJECTIVE: the day is about the platform.\n\n"
        "Today you have a meeting with the team and your manager, and it should be "
        "fine because the agenda is light between the two blocks of work today.\n\n"
        "AROUND: nothing else today.\n"
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "english_drift" in types


def test_english_text_not_flagged_in_english_run():
    text = (
        "OBJECTIVE: the day is about the platform.\n\n"
        "Today you have a meeting with the team and your manager. The agenda is "
        "light between the two blocks, so use the morning for the review and keep "
        "the afternoon for deep work on the migration plan before the deadline. "
        "The afternoon review feeds directly into the leadership sync, so carry "
        "the conclusions forward and flag the open decision on storage early.\n\n"
        "AROUND: nothing else demands action today.\n"
    )
    assert lint_vision(text, language="English") == []


def test_my_day_too_thin_flagged():
    text = "OBJECTIVE: une ligne.\n\nTrès court.\n\nAROUND: rien.\n"
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "my_day_too_thin" in types


def test_rogue_my_day_label_flagged():
    text = _GOOD_FR.replace("La colonne vertébrale du jour", "MY DAY La colonne vertébrale du jour")
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "rogue_my_day_label" in types


def test_rogue_heading_flagged():
    text = _GOOD_FR.replace(
        "Côté maison, deux choses à régler",
        "Suivi technique :\nCôté maison, deux choses à régler",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "rogue_heading" in types


def test_numbered_list_in_my_day_flagged():
    text = _GOOD_FR.replace(
        "La colonne vertébrale du jour",
        "1. La matinée technique\n2. L'après-midi RH\nLa colonne vertébrale du jour",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "numbered_list_in_my_day" in types


def test_readiness_figure_dump_flagged():
    text = _GOOD_FR.replace(
        "READINESS: Récupération correcte, garde le créneau du midi léger.",
        "READINESS: Récupération à 66%, HRV à 72 ms, sommeil 7h48 (95%), strain 9.4 hier.",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "readiness_figure_dump" in types


def test_readiness_with_one_figure_not_flagged():
    text = _GOOD_FR.replace(
        "READINESS: Récupération correcte, garde le créneau du midi léger.",
        "READINESS: Récupération à 66 % : base correcte, garde le créneau du midi léger.",
    )
    assert lint_vision(text, language="French") == []


def test_formal_address_flagged_in_french_run():
    text = _GOOD_FR.replace(
        "Prépare ce que tu veux faire remonter",
        "Préparez ce que vous voulez faire remonter avec votre équipe",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "formal_address" in types


def test_formal_address_ignores_pull_quotes_and_rendez_vous():
    text = _GOOD_FR.replace(
        "Côté maison, deux choses à régler",
        "> \"N'oubliez pas de compléter le fichier, vous avez jusqu'à demain\" — Orga, mail\n\n"
        "Ton rendez-vous de 14h précède tout. Côté maison, deux choses à régler",
    )
    assert lint_vision(text, language="French") == []


def test_readiness_helpers_roundtrip():
    from estormi_briefing.lint.vision_lint import readiness_has_figure_dump, readiness_line_span

    span = readiness_line_span(_GOOD_FR)
    assert span is not None
    start, end, content = span
    assert content.startswith("Récupération correcte")
    assert not readiness_has_figure_dump(content)
    assert readiness_has_figure_dump("66% de récup, HRV 72 ms, 7h48 de sommeil (95%)")
    assert readiness_line_span("pas de ligne readiness ici") is None


def test_empty_text_returns_no_issues():
    assert lint_vision("") == []
    assert lint_vision(None) == []  # type: ignore[arg-type]


def test_lede_issues_reject_mission_statements_and_keep_concrete_lines():
    from estormi_briefing.lint.vision_lint import lede_issues

    assert lede_issues("Concentrer l'énergie sur les engagements critiques du jour.")
    assert lede_issues("Optimiser la performance optimale en travail et en santé.")
    assert lede_issues("Une journée de travail bien remplie sans rien de spécial.")  # no anchor
    assert lede_issues("") == ["empty"]
    # Concrete: a time and a named event → clean.
    assert lede_issues("La journée se joue sur l'ADR Redshift à 10h avant le Comité data.") == []


def test_lint_vision_flags_coach_speak_in_my_day():
    from estormi_briefing.lint.vision_lint import lint_vision

    text = (
        "OBJECTIVE: x.\n\n"
        "La rétro de 14h sera plus utile si tu arrives avec des questions précises, "
        "et la suite de la journée s'organise autour des deux revues prévues qui "
        "occupent l'essentiel de l'après-midi avec leurs livrables respectifs à "
        "présenter aux équipes concernées par le programme en cours actuellement.\n\n"
        "AROUND: rien."
    )
    issues = lint_vision(text, language="French")
    assert any(i["type"] == "coach_speak" for i in issues)


def test_readiness_figure_dump_exempts_clock_times():
    from estormi_briefing.lint.vision_lint import readiness_has_figure_dump

    # Two health figures + a slot range: the clock times must not count.
    assert not readiness_has_figure_dump(
        "Récup 81% et sommeil 98% — ta séance passe sur le créneau 12:00–14:00."
    )
    assert readiness_has_figure_dump("Récup 81%, HRV 77 ms, strain 4.0 et 617 kcal.")


# ── stage_issues: the per-stage degeneration floor for the distillation eval ──


def test_stage_issues_lede_delegates_to_lede_check():
    """The lede keeps its richer anchor check (delegates to lede_issues)."""
    assert stage_issues("lede", "Réunion à 9h avec Diego, pivot sur le Data Lake.") == []
    # No proper noun → the lede-specific named-anchor check.
    assert any(
        "named anchor" in i
        for i in stage_issues("lede", "une journée comme les autres, sans rien de précis")
    )


def test_lede_requires_a_named_anchor_not_just_hours():
    """A clock time alone is not an anchor — the lede must name the pivot."""
    from estormi_briefing.lint.vision_lint import lede_issues

    # Bare time-span recital (the formulaic tic) — no named pivot → rejected.
    for tic in (
        "Une journée de 10h à 16h, avec un point fixe à 14h30, puis la revue de 15h à 16h.",
        "Une journée de 10h à 19h, avec le créneau 11h-12h comme point central.",
        "Samedi 16 juin, la journée est de 14h30 à 21h00, avec la revue à 21h.",
    ):
        issues = lede_issues(tic)
        assert any("named anchor" in i for i in issues), tic
    # Names a real pivot → accepted (hours present but not the only anchor).
    assert (
        lede_issues("Le daily Data Lake de 9h45 ouvre la journée, avant le point Clichy de 11h.")
        == []
    )


def test_stage_issues_flags_empty_and_clean_for_each_stage():
    for stage in ("readiness", "writer", "impact"):
        assert stage_issues(stage, "   ") == ["empty"]
    assert stage_issues("writer", "Le daily ouvre la journée ; prépare le budget avant midi.") == []
    assert stage_issues("impact", "→ Impact : ton crédit à Clichy se renchérit.") == []


def test_stage_issues_flags_coachspeak_english_and_lists():
    assert "coach-speak filler" in stage_issues("writer", "N'hésite pas à préparer le budget.")
    assert "English drift" in stage_issues(
        "writer", "The meeting with the team should start before the daily, and your day is dense."
    )
    assert "list formatting where prose is required" in stage_issues(
        "writer", "1. Préparer le budget\n2. Lancer le daily"
    )


def test_stage_issues_readiness_figure_dump_and_length():
    assert "recites too many health figures" in stage_issues(
        "readiness", "Récup 81%, HRV 77 ms, strain 4.0 et 617 kcal aujourd'hui."
    )
    assert "too long" in stage_issues("readiness", "x" * 401)


def test_melodrama_flagged_in_french_run():
    text = _GOOD_FR.replace(
        "Côté maison, deux choses à régler",
        "Avant que l'après-midi ne scelle leur sort, côté maison deux choses à régler",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "melodrama" in types


def test_melodrama_not_flagged_in_clean_briefing():
    types = {i["type"] for i in lint_vision(_GOOD_FR, language="French")}
    assert "melodrama" not in types


# ── D1: plural-imperative vouvoiement (curated list, not blanket \w+ez) ────────


def test_imperative_vous_flagged_as_formal_address():
    """A vous-form imperative ("Réservez…") carries no pronoun but still vouvoie."""
    text = _GOOD_FR.replace(
        "Prépare ce que tu veux faire remonter",
        "Réservez l'énergie pour la revue et planifiez la relance",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "formal_address" in types


def test_curated_imperative_list_does_not_eat_innocent_words():
    """The list is explicit — assez/chez/nez must not trip a \\w+ez false-positive."""
    from estormi_briefing.lint.vision_lint import _FRENCH_IMPERATIVE_VOUS_RE

    for innocent in ("assez tôt", "chez toi", "le nez au vent", "un rez-de-chaussée"):
        assert not _FRENCH_IMPERATIVE_VOUS_RE.search(innocent), innocent
    for imperative in ("Réservez l'énergie", "Planifiez la revue", "Vérifiez le budget"):
        assert _FRENCH_IMPERATIVE_VOUS_RE.search(imperative), imperative


# ── D2: personification melodrama, widened + fed to lede_issues ───────────────


def test_personification_melodrama_flagged_in_my_day():
    text = _GOOD_FR.replace(
        "les conclusions\nde l'une nourrissent l'autre",
        "la journée scelle la boucle et la revue tranche la trajectoire",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "melodrama" in types


def test_literal_sceller_un_accord_not_flagged():
    """ "sceller un accord/pacte" is plain language, not the personification tic."""
    from estormi_briefing.lint.vision_lint import _MELODRAMA_VERB_RE

    assert not _MELODRAMA_VERB_RE.search("sceller un accord avec le partenaire")
    assert not _MELODRAMA_VERB_RE.search("sceller le pacte de confiance")
    assert _MELODRAMA_VERB_RE.search("la journée scelle la boucle")


def test_lede_issues_flags_personification():
    from estormi_briefing.lint.vision_lint import lede_issues

    assert any("melodramatic" in i for i in lede_issues("La journée scelle la boucle avec Diego."))
    # A named pivot stated soberly (and "sceller un accord" as a real action) passes.
    assert lede_issues("Réunion avec Diego pour sceller un accord commercial à 14h.") == []


# ── D3: [src: …] marker buried mid-sentence in MY DAY ─────────────────────────


def test_src_marker_mid_sentence_flagged():
    text = _GOOD_FR.replace(
        "puis la seconde session de la revue avec le partenaire de 15h à 17h",
        "puis la revue [src: agenda · 12 juin] avec le partenaire de 15h à 17h",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "src_marker_mid_sentence" in types


def test_src_marker_at_line_end_not_flagged():
    """AROUND bullets legitimately CLOSE on their [src: …] — never mid-sentence."""
    types = {i["type"] for i in lint_vision(_GOOD_FR, language="French")}
    assert "src_marker_mid_sentence" not in types


# ── D6: MY DAY self-repetition (repeated content bigrams) ─────────────────────


def test_my_day_self_repetition_flagged():
    text = _GOOD_FR.replace(
        "La colonne vertébrale du jour, c'est la plateforme data. Deux heures d'ADR dès 10h,\n"
        "puis la seconde session de la revue avec le partenaire de 15h à 17h : les conclusions\n"
        "de l'une nourrissent l'autre, et tout débouche sur le point leadership à 17h.",
        "La revue partenaire prépare le point leadership. La revue partenaire nourrit le "
        "point leadership. La revue partenaire ouvre le point leadership du soir sans faute.",
    )
    types = {i["type"] for i in lint_vision(text, language="French")}
    assert "my_day_self_repetition" in types


def test_ordinary_prose_not_flagged_as_self_repetition():
    types = {i["type"] for i in lint_vision(_GOOD_FR, language="French")}
    assert "my_day_self_repetition" not in types


def test_repeated_content_bigrams_ignores_stopwords():
    from estormi_briefing.lint.vision_lint import repeated_content_bigrams

    # "de la" / "et le" recur but are pure stopwords → no content repetition.
    assert repeated_content_bigrams("il part de la gare et le train arrive de la ville") == 0
    # Three distinct content bigrams repeated verbatim.
    assert (
        repeated_content_bigrams(
            "revue partenaire point leadership, revue partenaire point leadership encore"
        )
        >= 3
    )
