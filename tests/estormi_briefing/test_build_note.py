"""build_daily_note — HTML rendering of the daily briefing."""

from __future__ import annotations

import pytest

from estormi_briefing.compose.build_daily_note import (
    _news_bullets_to_html,
    _render_themes_html,
    _theme_source_color,
    build_note,
)
from estormi_briefing.compose.prompts import _themes_prompt

pytestmark = pytest.mark.unit

# ── build_daily_note ──────────────────────────────────────────────────────────


def test_build_note_structure():
    body = build_note(
        "2026-05-02",
        source_count=6,
        video_count=2,
        news_synthesis="- Apple annonce un partenariat. [SOURCE: NowTech | 2026-05-02]",
        themes_html="<p><b>🤖 Tech</b></p><p>La tech francaise est absente du debat.</p>",
    )

    assert 'class="briefing-title">Briefing' in body and "May 2, 2026" in body
    assert "Today's news" in body
    assert "Watch" in body
    assert "Apple annonce" in body
    assert "tech francaise" in body
    assert "Estormi — Briefing" in body
    assert "6 channels" in body and "2 new" in body


def test_build_note_omits_empty_sections():
    body = build_note("2026-05-02", 1, 1)
    assert "Finance" not in body
    assert "À réfléchir" not in body
    assert "Today's news" not in body
    assert "Watch" not in body


def test_build_note_empty_items():
    body = build_note("2026-05-02", 6, 0)
    assert "May 2, 2026" in body
    assert "6 channels" in body and "0 new" in body


def test_build_note_daily_actions_section():
    body = build_note(
        "2026-05-03",
        6,
        0,
        actions={
            "calendar": [{"when": "09:30", "title": "Point projet", "overdue": False}],
            "reminders": [
                {
                    "when": "Toute la journée",
                    "title": "Envoyer la facture",
                    "overdue": False,
                }
            ],
        },
    )

    assert "My day" in body
    assert "<b>Schedule</b>" in body
    assert "<b>Don't forget</b>" in body
    assert "<b>09:30</b>" in body
    assert "Point projet" in body
    assert "Envoyer la facture" in body


def test_build_note_actions_overdue_marked():
    """Overdue reminders show the ⚠ marker."""
    body = build_note(
        "2026-05-03",
        1,
        0,
        actions={
            "calendar": [],
            "reminders": [
                {
                    "when": "Toute la journée",
                    "title": "Overdue task",
                    "overdue": True,
                },
                {
                    "when": "Toute la journée",
                    "title": "Tâche du jour",
                    "overdue": False,
                },
            ],
        },
    )

    assert "⚠" in body
    assert "Overdue task" in body
    # Today's reminder must NOT have the overdue marker
    idx_retard = body.index("Overdue task")
    assert "⚠" in body[:idx_retard]
    idx_jour = body.index("Tâche du jour")
    # No ⚠ immediately before "Tâche du jour"
    assert "⚠" not in body[idx_jour - 20 : idx_jour]


def test_build_note_actions_empty_dict_shows_nothing():
    """Empty actions dict produces no actions section."""
    body = build_note(
        "2026-05-03",
        1,
        0,
        actions={"calendar": [], "reminders": []},
    )

    assert "My day" not in body


def test_build_note_veille_section_shown_when_themes_html():
    body = build_note("2026-05-02", 1, 1, themes_html="<p><b>🤖 AI</b></p><p>AI is overhyped.</p>")
    assert "Watch" in body
    assert "AI is overhyped" in body


# ── run_knowledge: build_note HTML output ─────────────────────────────────────


def test_build_note_html_bullets():
    """Bullets are wrapped in <ul><li> tags."""
    body = build_note(
        "2026-05-03",
        1,
        1,
        news_synthesis="- Fact one. [SOURCE: NowTech | 2026-05-03]\n- Fact two. [SOURCE: NowTech | 2026-05-03]",
    )
    assert "<ul>" in body
    assert "<li>" in body
    assert "Fact one." in body
    assert "Fact two." in body


def test_news_bullets_dedup_near_duplicates():
    """Weak local models repeat items; identical/near-identical bullets collapse to one."""
    text = (
        "- Cleanup op gathered 25000 volunteers in PACA. [SOURCE: X | 2026-06-03]\n"
        "- Cleanup op gathered 25000 volunteers in PACA! [SOURCE: X | 2026-06-03]\n"
        "- A different story entirely. [SOURCE: Y | 2026-06-03]"
    )
    html = "".join(_news_bullets_to_html(text))
    assert html.count("<li>") == 2  # the duplicate is dropped
    assert "different story" in html


def test_news_bullets_strip_leaked_kind_tag():
    """A leaked [concept]/[prediction]… schema tag is stripped from the bullet."""
    text = "- [concept] Scaffolding matters more than the model. [SOURCE: X | 2026-06-03]"
    html = "".join(_news_bullets_to_html(text))
    assert "[concept]" not in html
    assert "Scaffolding matters" in html


def test_render_themes_unwraps_kind_colon_tag():
    """A wrapped ``[insight : foo]`` schema title is unwrapped to ``foo``."""
    text = "THEME: 💰 Finance\n[insight : Domination de Reliance en Inde]"
    html = _render_themes_html(text)
    assert "[insight" not in html
    assert "Domination de Reliance en Inde" in html


def test_render_themes_strips_leaked_kind_tag():
    """Leaked schema tags are stripped from theme titles and paragraphs."""
    text = "THEME: 🤖 [concept] AI tooling\n[insight] The harness is decisive."
    html = _render_themes_html(text)
    assert "[concept]" not in html
    assert "[insight]" not in html
    assert "AI tooling" in html


def test_strip_vision_scaffolding_headers_and_doubled_readiness():
    """Markdown headers, doubled READINESS:, and invented section titles are removed."""
    from estormi_briefing.compose.build_daily_note import _strip_vision_scaffolding

    raw = "### READINESS: READINESS: Recovery is good.\n\n### MORNING BRIEFING:\nYour day is busy."
    cleaned = _strip_vision_scaffolding(raw)
    assert "###" not in cleaned
    assert "MORNING BRIEFING" not in cleaned
    assert cleaned.count("READINESS:") == 1
    assert "Recovery is good." in cleaned
    assert "Your day is busy." in cleaned


def test_strip_vision_scaffolding_keeps_readiness_for_card():
    """A cleaned doubled-READINESS line still feeds the readiness card."""
    from estormi_briefing.compose.build_daily_note import (
        _split_readiness,
        _strip_vision_scaffolding,
    )

    readiness, body = _split_readiness(
        _strip_vision_scaffolding("### READINESS: READINESS: 68% recovery.\n\nThe day ahead.")
    )
    assert readiness == "68% recovery."
    assert "The day ahead." in body


def test_build_note_footer_names_model():
    """The composing model is attributed in the footer when provided."""
    body = build_note("2026-05-03", 1, 1, model_label="local/qwen14b")
    assert "Composed by local/qwen14b" in body


def test_build_note_footer_omits_model_when_absent():
    """No attribution line when no model_label is passed (back-compat)."""
    body = build_note("2026-05-03", 1, 1)
    assert "Composed by" not in body


def test_build_note_footer_shows_generation_time():
    """The generation time sits right after the date in the footer."""
    body = build_note("2026-05-03", 1, 1, composed_at="09:24")
    assert "at 09:24" in body
    # French chrome localises the time label.
    body_fr = build_note("2026-05-03", 1, 1, composed_at="09:24", lang="fr")
    assert "à 09:24" in body_fr


def test_build_note_footer_omits_time_when_absent():
    """No time fragment when composed_at is not passed (back-compat)."""
    body = build_note("2026-05-03", 1, 1)
    assert "at " not in body.split('class="b-footer"')[-1]


def test_build_note_html_sections():
    """Section headings use <h2> for section labels."""
    body = build_note(
        "2026-05-03",
        1,
        1,
        themes_html="<p><b>🤖 AI — Src</b></p><p>Detail about AI.</p>",
    )
    assert "Watch" in body
    assert "<h2" in body


def test_build_note_footer_italic():
    """Footer is wrapped in <i> for subtle styling."""
    body = build_note("2026-05-03", 3, 0)
    assert "<i>" in body
    assert "Estormi — Briefing" in body


def test_build_note_html_escapes_special_chars():
    """User-supplied content with < > & is HTML-escaped."""
    body = build_note(
        "2026-05-03", 1, 1, news_synthesis="- A < B & C > D [SOURCE: NowTech | 2026-05-03]"
    )
    assert "&lt;" in body
    assert "&gt;" in body
    assert "&amp;" in body


def test_build_note_vision_leads_and_strips_html():
    """The day-vision is plain-text markers: Python owns every tag, and any
    HTML the model emits (including script content) is stripped."""
    body = build_note(
        "2026-05-03",
        1,
        0,
        actions={"calendar": [{"when": "09:00", "title": "Réunion"}]},
        vision_html="The day turns on **one decision**.<script>alert(1)</script>",
    )

    assert "<script>" not in body
    assert "alert(1)" not in body
    # **bold** is promoted; the narrative leads (right after the <h1>).
    assert "<p>The day turns on <b>one decision</b>.</p>" in body
    assert body.index("<p>The day turns on") < body.index("Estormi — Briefing")
    # The robotic intro line is gone on the editorial path.
    assert "Briefing for May" not in body


def test_build_note_readiness_card_opens_briefing():
    """A leading `READINESS:` line is lifted into the health card at the top and
    removed from the prose; the prose drop cap is preserved (card is div-only)."""
    body = build_note(
        "2026-06-02",
        1,
        0,
        actions={"calendar": [{"when": "09:00", "title": "Sprint"}]},
        vision_html=(
            "READINESS: Recovery is green after a yellow Monday — a good day to "
            "take on load. [src: WHOOP]\n\n"
            "The day turns on **one decision**."
        ),
    )

    # Card present, opens before the prose, and carries the steer …
    assert 'class="readiness"' in body
    assert "✦ Readiness" in body
    assert "a good day to take on load" in body
    assert body.index('class="readiness"') < body.index("<p>The day turns on")
    # … the sentinel + its [src:] are gone from the prose, no literal "READINESS:".
    assert "READINESS:" not in body
    # The card is div-only so it can't steal the `p:first-of-type` drop cap —
    # the first <p> in the body is still the vision lead.
    assert body.index("<p>The day turns on <b>one decision</b>.</p>") > 0
    assert "<p>" not in body[body.index('class="readiness"') : body.index("<p>The day turns on")]


def test_build_note_readiness_card_tolerates_space_before_colon():
    """The model sometimes slips into French typography (`READINESS :`) — the
    steer must still be lifted into the card, not bleed into the prose."""
    body = build_note(
        "2026-06-02",
        1,
        0,
        actions={"calendar": [{"when": "09:00", "title": "Sprint"}]},
        vision_html=(
            "READINESS : Récupération correcte à 68 %, sommeil solide.\n\n"
            "The day turns on **one decision**."
        ),
    )
    assert 'class="readiness"' in body
    assert "✦ Readiness" in body
    assert "Récupération correcte à 68 %" in body
    assert "READINESS" not in body[: body.index("<p>The day turns on")].replace("✦ Readiness", "")


def test_build_note_no_readiness_line_means_no_card():
    body = build_note(
        "2026-06-02",
        1,
        0,
        actions={"calendar": [{"when": "09:00", "title": "Sprint"}]},
        vision_html="The day turns on **one decision**.",
    )
    assert 'class="readiness"' not in body
    assert "✦ Readiness" not in body


def test_render_vision_html_pull_quote_and_attribution():
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html(
        "The move-out logistics with Camille have **hardened**. [src: mail · 06:12]\n"
        "\n"
        '> "Can you let me know about the deposit by the weekend?" — Camille, WhatsApp'
    )

    # Paragraph attribution → gold source span.
    assert "<b>hardened</b>" in html
    assert "mail · 06:12" in html
    assert 'class="source"' in html
    assert "[src:" not in html  # marker consumed, never leaked
    # Pull-quote → styled blockquote with the verbatim and its attribution.
    assert "<blockquote" in html
    assert "the deposit by the weekend" in html
    assert "Camille, WhatsApp" in html


def test_render_vision_html_chained_attributions():
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html(
        "Le sujet du jour, c'est la décision. "
        "[src: WhatsApp · Camille · 2 Jun] [src: rappel · 2 Jun] [src: agenda · 3 Jun]"
    )

    # Every marker becomes a gold source span, in original order — none leaks.
    assert "[src:" not in html
    assert html.count('class="source"') == 3
    assert (
        html.index("WhatsApp · Camille · 2 Jun")
        < html.index("rappel · 2 Jun")
        < html.index("agenda · 3 Jun")
    )


def test_render_vision_html_escapes_content():
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html("A < B & C > D")
    assert "&lt;" in html
    assert "&amp;" in html
    assert "&gt;" in html


def test_render_vision_html_empty():
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    assert _render_vision_html("") == ""
    assert _render_vision_html("   \n\n  ") == ""


def test_render_vision_html_drops_markdown_horizontal_rules():
    """Weak models (Ministral) sprinkle `---` rules between paragraphs — both
    standalone and leading real prose. Opus omits them; the renderer must drop
    them either way so they never reach the page as literal dashes / empty <p>."""
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html("---\n\nFirst para.\n\n--- Second para here.")
    assert "<p>---</p>" not in html
    assert "--- " not in html
    assert "<p>First para.</p>" in html
    assert "<p>Second para here.</p>" in html


def test_render_vision_html_drops_hr_line_inside_block():
    """A `---` line sandwiched between prose lines of the SAME block used to
    survive the prefix-only strip and get joined into the paragraph as
    literal dashes."""
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html("First line.\n---\nSecond line same block.")
    assert "---" not in html
    # The rule line becomes a paragraph break — the model meant a separator.
    assert "<p>First line.</p>" in html
    assert "<p>Second line same block.</p>" in html


def test_esc_with_md_drops_orphan_bold_marker():
    """An unclosed `**` (the model opened bold and never closed it) must not
    ship as literal asterisks."""
    from estormi_briefing.compose.build_daily_note import _esc_with_md

    assert _esc_with_md("la feuille de route technique** pour aligner") == (
        "la feuille de route technique pour aligner"
    )
    # Balanced pairs still promote to <b>.
    assert _esc_with_md("un **mot** clé") == "un <b>mot</b> clé"


def test_news_bullets_keep_only_first_calendar_flag():
    """📅 is a schedule signal, meaningful once — a weak model stamps it on
    half the section. Keep the first, strip the rest; ↩ is never touched."""
    from estormi_briefing.compose.build_daily_note import _news_bullets_to_html

    text = (
        "- 📅 Premier item lié à l'agenda [SOURCE: Le Monde | 2026-06-11]\n"
        "- 📅 Deuxième item flaggé à tort [SOURCE: Le Monde | 2026-06-11]\n"
        "- ↩ Follow-up: suite d'hier [SOURCE: Le Monde | 2026-06-11]\n"
    )
    html = "\n".join(_news_bullets_to_html(text))
    assert html.count("📅") == 1
    assert "↩" in html


def test_render_vision_html_converts_markdown_emphasis():
    """`**bold**` → <b>, `*italic*` → <i>. Ministral leans on both; without
    conversion the raw asterisks render literally."""
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html("The *solution vision* drives **Friday** here.")
    assert "<i>solution vision</i>" in html
    assert "<b>Friday</b>" in html
    assert "*" not in html


def test_esc_with_md_leaves_arithmetic_asterisks_alone():
    """A bare `*` with surrounding whitespace (e.g. "2 * 3") is not emphasis and
    must survive untouched — the italic rule only fires on word-hugging `*`."""
    from estormi_briefing.compose.build_daily_note import _esc_with_md

    assert _esc_with_md("2 * 3 = 6") == "2 * 3 = 6"


# ── build_daily_note: news_synthesis rendering ────────────────────────────────


def test_build_note_with_news_synthesis_shows_actualites_section():
    synthesis = (
        "- Ukraine en tension. [SOURCE: Le Monde · Hugo Décrypte | 2026-05-15]\n"
        "- Hausse des taux annoncée. [SOURCE: Le Monde | 2026-05-15]"
    )
    themes = "<p><b>🔬 Tech</b></p><p>Analyse approfondie.</p>"

    body = build_note("2026-05-15", 5, 10, news_synthesis=synthesis, themes_html=themes)

    assert "Today's news" in body
    assert "Ukraine en tension" in body
    assert "Hausse des taux" in body
    assert "Watch" in body
    assert "Analyse approfondie" in body


def test_build_note_without_news_synthesis_shows_only_veille():
    body = build_note(
        "2026-05-15",
        1,
        1,
        news_synthesis="",
        themes_html="<p><b>🔬 Tech</b></p><p>Fait.</p>",
    )

    assert "Today's news" not in body
    assert "Watch" in body


def test_build_note_shows_veille_thematique_heading():
    body = build_note(
        "2026-05-15",
        1,
        1,
        themes_html="<p><b>🔬 Tech — T</b></p><p>Insight.</p>",
    )

    assert "Watch" in body
    assert "Insight" in body


def test_build_note_news_synthesis_html_escaped():
    body = build_note(
        "2026-05-15", 1, 0, news_synthesis="- A < B & C > D [SOURCE: NowTech | 2026-05-15]"
    )

    assert "&lt;" in body
    assert "&gt;" in body
    assert "&amp;" in body


def test_news_bullets_to_html_renders_source_marker():
    lines = _news_bullets_to_html(
        "- Dette française au plus haut. [SOURCE: Le Monde]\n"
        "- Ukraine : 24 morts. [SOURCE: Le Monde · Hugo Décrypte]\n"
    )
    html = "\n".join(lines)

    # Sources are colour-coded deterministically from the shared palette.
    assert _theme_source_color("Le Monde") in html
    assert _theme_source_color("Hugo Décrypte") in html
    assert "Dette française" in html
    assert "Ukraine" in html
    assert "[SOURCE:" not in html


def test_news_bullets_to_html_no_marker_renders_plain():
    """A markerless bullet is an upstream anomaly now that attribution is carried
    in code (resolve_news_citations drops uncited bullets; fallback re-attaches
    the source). It still renders, but plainly — no "(source?)" hint leaks into
    the shipped briefing."""
    lines = _news_bullets_to_html(
        "- Real item. [SOURCE: Le Monde | 2026-05-15]\n- Bullet without source marker\n"
    )
    html = "\n".join(lines)

    assert "Real item." in html
    assert "Bullet without source marker" in html
    assert "(source?)" not in html


def test_news_bullets_to_html_renders_three_sources():
    """Triple cross-reference: all three sources colour-coded and printed."""
    lines = _news_bullets_to_html(
        "- Sujet majeur cross-référencé. [SOURCE: Le Monde · Hugo Décrypte · Future Source]\n"
    )
    html = "\n".join(lines)

    assert html.count("<span") >= 3
    assert "Le Monde" in html
    assert "Hugo Décrypte" in html
    assert "Future Source" in html
    # All three source separators rendered.
    assert html.count(" · ") >= 2


def test_news_bullets_to_html_unknown_source_gets_palette_color():
    """Any source — known brand or not — gets a deterministic palette colour;
    there are no hardcoded brand colours anymore."""
    lines = _news_bullets_to_html("- Some news item. [SOURCE: Future Source]\n")
    html = "\n".join(lines)

    assert _theme_source_color("Future Source") in html
    assert "Future Source" in html


def test_news_bullets_to_html_escapes_content():
    lines = _news_bullets_to_html("- A < B & C. [SOURCE: Le Monde]\n")
    html = "\n".join(lines)

    assert "&lt;" in html
    assert "&amp;" in html


def test_news_bullets_to_html_renders_source_date():
    """A '| date' suffix in the SOURCE marker renders as a muted long-form date."""
    lines = _news_bullets_to_html("- Sujet daté. [SOURCE: Le Monde · Hugo Décrypte | 2026-06-01]\n")
    html = "\n".join(lines)

    assert _theme_source_color("Le Monde") in html
    assert _theme_source_color("Hugo Décrypte") in html
    # The date renders long-form, not as the raw ISO string or pipe.
    assert "June 1, 2026" in html
    assert "|" not in html
    assert "2026-06-01" not in html


def test_news_bullets_to_html_no_date_unchanged():
    """Without a '| date' suffix the marker renders exactly as before."""
    lines = _news_bullets_to_html("- Sujet sans date. [SOURCE: Le Monde]\n")
    html = "\n".join(lines)

    assert "Le Monde" in html
    assert "|" not in html


# NOTE: test_news_synthesis_prompt_requests_6_to_10_items was deleted as a
# strict subset of test_news_synthesis_prompt_signal_content_logic +
# test_news_synthesis_prompt_includes_a_noter_aussi_section.


# ── build_daily_note: themes_html rendering ───────────────────────────────────


def test_build_note_themes_html_renders_under_veille():
    """themes_html (structured text) renders under Themes, with
    no legacy section headers and no raw script tags. (Merged from two
    near-duplicate tests during the v1.8 quality sweep.)"""
    themes = (
        "THÈME: 🤖 IA\n"
        "SOURCE: Nate B Jones | OpenAI price cuts | 2026-05-15\n"
        "OpenAI et Claude se livrent une course aux benchmarks."
    )
    body = build_note("2026-05-15", 5, 10, themes_html=themes)

    assert "Watch" in body
    assert "🤖 IA" in body
    assert "OpenAI" in body
    assert "Nate B Jones" in body
    assert "<script>" not in body
    # Legacy section headers must not appear in the new structured layout.
    assert "Insights 💡" not in body
    assert "Ce qu'il faut savoir" not in body


def test_build_note_no_veille_when_themes_html_empty():
    """Without themes_html, the Themes section is omitted."""
    body = build_note("2026-05-15", 1, 1, themes_html="")

    assert "Watch" not in body


# ── build_daily_note: _theme_source_color ────────────────────────────────────


def test_theme_source_color_is_deterministic():
    assert _theme_source_color("Nate B Jones") == _theme_source_color("Nate B Jones")
    assert _theme_source_color("Nate B Jones") == _theme_source_color("nate b jones")


def test_theme_source_color_differs_across_sources():
    colors = {_theme_source_color(s) for s in ("Nate B Jones", "WelchLabs", "Finary", "Hasheur")}
    assert len(colors) >= 2


def test_theme_source_color_returns_valid_hex():
    color = _theme_source_color("Any Source")
    assert color.startswith("#")
    assert len(color) == 7


# ── build_daily_note: _render_themes_html ─────────────────────────────────────


def test_render_themes_html_heading():
    result = _render_themes_html("THÈME: 🤖 Intelligence Artificielle\n")
    assert "<p><b>🤖 Intelligence Artificielle</b></p>" in result


def test_render_themes_html_source_line():
    from estormi_briefing.compose.build_daily_note import (
        _render_themes_html,
        _theme_source_color,
    )

    result = _render_themes_html(
        "THÈME: 🤖 IA\nSOURCE: Nate B Jones | OpenAI news | 2026-05-15\nRésumé du contenu.\n"
    )
    color = _theme_source_color("Nate B Jones")
    assert f"color:{color}" in result
    assert "<b>Nate B Jones</b>" in result
    assert "OpenAI news" in result
    assert "May 15, 2026" in result
    assert "<p>Résumé du contenu.</p>" in result
    # Source span must appear AFTER the content paragraph
    assert result.index("<p>Résumé du contenu.</p>") < result.index("<b>Nate B Jones</b>")
    # Spacing between blocks
    assert "<p>&nbsp;</p>" in result


def test_render_themes_html_multi_source_same_theme():
    text = (
        "THÈME: 🤖 IA\n"
        "SOURCE: Nate B Jones | Titre A | 2026-05-15\n"
        "Résumé Nate.\n\n"
        "SOURCE: NowTech | Titre B | 2026-05-15\n"
        "Résumé NowTech.\n"
    )
    result = _render_themes_html(text)
    assert "Nate B Jones" in result
    assert "NowTech" in result
    assert "Résumé Nate." in result
    assert "Résumé NowTech." in result
    assert result.count("🤖 IA") == 1


def test_render_themes_html_escapes_content():
    result = _render_themes_html(
        "THÈME: A & B\nSOURCE: Src | T | 2026-05-15\n<script>evil()</script>\n"
    )
    assert "&amp;" in result  # ampersand in heading escaped
    assert "&lt;" in result  # < in content escaped — script tag cannot execute
    assert "<script>" not in result  # raw script tag must never appear


def test_render_themes_html_empty_input():
    assert _render_themes_html("") == ""
    assert _render_themes_html(None) == ""  # type: ignore[arg-type]


def test_themes_prompt_uses_theme_source_markers():
    items = [{"source_label": "Nate B Jones", "bullets": ["- AI news."]}]
    prompt = _themes_prompt(items, "2026-05-15")

    assert "THEME:" in prompt
    assert "SOURCE:" in prompt
    assert "Nate B Jones" in prompt
    assert "SOURCE_ATTR" not in prompt


def test_themes_prompt_forbids_inline_source_name():
    """Prompt must instruct LLM not to repeat source name in the summary text."""
    items = [{"source_label": "Nate B Jones", "bullets": ["- AI news."]}]
    prompt = _themes_prompt(items, "2026-05-15")
    # Explicit rule must be present
    assert (
        "never repeat the source name" in prompt.lower()
        or "only on the source: line" in prompt.lower()
    )


def test_render_themes_html_source_without_date():
    """SOURCE line with only label|title (no date) must render cleanly — no raw text leak."""
    text = "THÈME: 🤖 IA\nSOURCE: AI News & Strategy Daily | Nate B Jones\nRésumé du contenu IA.\n"
    result = _render_themes_html(text)
    # Source label rendered as formatted span
    assert "<b>AI News &amp; Strategy Daily</b>" in result
    # Raw SOURCE: line must NOT appear verbatim in output
    assert "SOURCE:" not in result
    # Content paragraph present
    assert "Résumé du contenu IA." in result


def test_render_themes_html_source_label_only():
    """SOURCE line with just a label (no pipes) must not leak as content text."""
    text = "THÈME: 💰 Crypto\nSOURCE: Hasheur\nContenu crypto ici.\n"
    result = _render_themes_html(text)
    assert "<b>Hasheur</b>" in result
    assert "SOURCE:" not in result
    assert "Contenu crypto ici." in result


def test_render_themes_html_no_duplicate_source_label():
    """Source label must appear exactly once (in the span), not also in content."""
    # Simulate LLM that repeats source name in the content (shouldn't happen but
    # we verify the renderer at least doesn't add an extra copy itself).
    text = (
        "THÈME: 🤖 IA\nSOURCE: Nate B Jones | Titre | 2026-05-15\nRésumé sans répéter la source.\n"
    )
    result = _render_themes_html(text)
    # "Nate B Jones" must appear exactly once (inside the colored span)
    assert result.count("Nate B Jones") == 1


def test_render_themes_html_multi_source_with_missing_date():
    """Multi-source block where one SOURCE has no date renders both sources."""
    text = (
        "THÈME: 🤖 IA\n"
        "SOURCE: Nate B Jones | OpenAI news | 2026-05-15\n"
        "Résumé Nate.\n\n"
        "SOURCE: NowTech | Autre vidéo\n"
        "Résumé NowTech.\n"
    )
    result = _render_themes_html(text)
    assert "Nate B Jones" in result
    assert "NowTech" in result
    assert "SOURCE:" not in result  # no raw SOURCE: lines leaked


def test_split_readiness_tolerates_markdown_label():
    """Local models sometimes wrap the steer in markdown (**READINESS:**); it
    must still lift into the health card, not bleed into the prose."""
    from estormi_briefing.compose.build_daily_note import _split_readiness

    r, body = _split_readiness("**READINESS:** Recovery 57%.\n\nThe day ahead.")
    assert r == "Recovery 57%."
    assert "The day ahead." in body


# ── Five-section structure + localisation (the UX redesign) ───────────────────


def test_briefing_title_localised():
    from estormi_briefing.compose.build_daily_note import briefing_title

    assert briefing_title("2026-06-05", "en") == "Briefing — June 5, 2026"
    assert briefing_title("2026-06-05", "fr") == "Briefing du 5 juin 2026"
    # Unknown code falls back to English chrome.
    assert briefing_title("2026-06-05", "de") == "Briefing — June 5, 2026"


def test_split_objective_lifts_leading_line():
    from estormi_briefing.compose.build_daily_note import _split_objective

    obj, rest = _split_objective("OBJECTIVE: A data-dense Friday.\n\nThe day proper.")
    assert obj == "A data-dense Friday."
    assert "The day proper." in rest
    # Absent sentinel → unchanged.
    assert _split_objective("No sentinel here.") == ("", "No sentinel here.")


def test_split_around_splits_on_sentinel():
    from estormi_briefing.compose.build_daily_note import _split_around

    my_day, around = _split_around("The core narrative.\n\nAROUND:\nOrbit.\n- item [src: mail]")
    assert "core narrative" in my_day and "AROUND" not in my_day
    assert "Orbit." in around and "item" in around
    # Absent sentinel → all is my-day.
    assert _split_around("Just the day.") == ("Just the day.", "")


def test_render_around_hybride_intro_plus_bullets():
    from estormi_briefing.compose.build_daily_note import _render_around_html

    html = _render_around_html(
        "A few threads orbit the day.\n"
        "- Validate the quote [src: mail · 31 May]\n"
        "- Lunch Saturday [src: gcal · 6 Jun]"
    )
    assert "<p>A few threads orbit the day.</p>" in html
    assert html.count("<li>") == 2
    assert "Validate the quote" in html
    # Trailing [src: …] becomes a gold attribution span, not literal brackets.
    assert "[src:" not in html
    assert 'class="source"' in html


def test_build_note_editorial_five_sections_fr():
    """The editorial path emits the five value-oriented sections, localised."""
    vision = (
        "READINESS: Recup a 57%, journee de maintenance.\n\n"
        "OBJECTIVE: Vendredi data-dense.\n\n"
        "Le coeur de la journee. [src: gcal]\n\n"
        "AROUND:\nQuelques fils orbitent.\n- Devis a valider [src: mail]"
    )
    body = build_note(
        "2026-06-05",
        8,
        1,
        vision_html=vision,
        news_synthesis="- Loi adoptee. [SOURCE: Le Monde | 2026-06-04]",
        rss_articles=48,
        youtube_videos=1,
        model_label="claude-cli/opus",
        lang="fr",
    )
    # Title + objective subtitle
    assert 'class="briefing-title">Briefing du 5 juin 2026' in body
    assert 'class="briefing-objective"' in body and "Vendredi data-dense" in body
    # Readiness card, localised label
    assert "Forme du jour" in body and "journee de maintenance" in body
    # The three content sections, in order, with their wrapper classes
    assert (
        body.index('class="b-day"') < body.index('class="b-around"') < body.index('class="b-world"')
    )
    assert "Ma journée" in body and "Autour de ma journée" in body and "Le monde" in body
    # Localised footer + world date
    assert "Composé par claude-cli/opus" in body
    assert "dernières 24 h" in body and "48 article(s) RSS" in body
    assert "4 juin 2026" in body  # news date localised to FR


def test_build_note_drop_cap_scopes_to_my_day():
    """The first My-day paragraph is a direct child of .b-day so the shared CSS
    drop cap lands there, not on the objective subtitle or readiness card."""
    body = build_note(
        "2026-06-05",
        1,
        0,
        vision_html="OBJECTIVE: The thread.\n\nThe first real paragraph.",
        lang="en",
    )
    assert '<section class="b-day">' in body
    # h2, the (element-invisible) my-day zone marker, then the first <p> as a
    # direct child of the section. The HTML comment is not an element, so the CSS
    # rule `.b-day > p:first-of-type::first-letter` still lands the lettrine on
    # this paragraph — the field editor's splice markers don't disturb the cap.
    assert "<h2>📅 My day</h2>\n<!--myday:start--><p>The first real paragraph." in body
    # The objective sits OUTSIDE .b-day (so it never steals the drop cap).
    assert body.index('class="briefing-objective"') < body.index('class="b-day"')


def test_briefing_fields_round_trip_is_identity():
    """Extracting each section's source then splicing it straight back must leave
    the composed body byte-for-byte unchanged — the field editor's no-op save."""
    from estormi_briefing.compose.build_daily_note import briefing_fields, splice_section

    vision = (
        "READINESS: short night but efficient sleep.\n\n"
        "OBJECTIVE: A preparation day.\n\n"
        "The session at 2pm is the only window.\n\n"
        "A **second** paragraph of insight.\n\n"
        "AROUND:\n- A world note [src: rss]"
    )
    body = build_note("2026-06-14", 3, 2, vision_html=vision, lang="en")
    fields = briefing_fields(vision)
    out = body
    for name in ("readiness", "objective", "myDay"):
        out = splice_section(out, name, fields[name], "en")
        assert out is not None, f"no markers for {name}"
    assert out == body


def test_splice_section_targets_one_section():
    """Editing one section re-renders only its region; the others stay intact."""
    from estormi_briefing.compose.build_daily_note import splice_section

    vision = "READINESS: ok recovery.\n\nOBJECTIVE: first.\n\nThe narrative paragraph."
    body = build_note("2026-06-14", 1, 0, vision_html=vision, lang="en")
    out = splice_section(body, "objective", "second objective", "en")
    assert out is not None and "second objective" in out
    assert "first" not in out  # old objective replaced
    assert "The narrative paragraph." in out  # my-day untouched
    assert "ok recovery" in out  # readiness untouched
    assert out.count("<!--objective:start-->") == 1  # markers not duplicated


def test_splice_section_missing_markers_returns_none():
    """A body without the section markers (pre-editor briefing) yields None so the
    caller can fall back to a raw-HTML edit."""
    from estormi_briefing.compose.build_daily_note import splice_section

    assert splice_section("<p>plain</p>", "objective", "x", "en") is None
    assert splice_section("<p>plain</p>", "myDay", "x", "en") is None
    assert splice_section("<p>plain</p>", "readiness", "x", "en") is None


def test_render_vision_html_renders_mid_paragraph_attribution():
    """A `[src: …]` marker dropped mid-sentence (not just at the paragraph end)
    must still render as a gold span, never leak as literal brackets."""
    from estormi_briefing.compose.build_daily_note import _render_vision_html

    html = _render_vision_html(
        "Validate the quote today [src: mail · 31 May] then start the laundry."
    )
    assert "[src:" not in html
    assert 'class="source"' in html
    # The text on both sides of the marker survives.
    assert "Validate the quote today" in html
    assert "then start the laundry." in html


def test_render_around_html_renders_mid_bullet_attribution():
    from estormi_briefing.compose.build_daily_note import _render_around_html

    html = _render_around_html("- Email ClubTidy [src: reminder · 7 Jun] about the keys.")
    assert "[src:" not in html
    assert 'class="source"' in html
    assert "about the keys." in html


def test_long_date_french_first_of_month_uses_ordinal():
    """French dates: the 1st takes the "1er" ordinal; other days are bare."""
    from estormi_briefing.compose.build_daily_note import _long_date

    assert _long_date("2026-06-01", "fr") == "1er juin 2026"
    assert _long_date("2026-06-02", "fr") == "2 juin 2026"
    assert _long_date("2026-06-01", "en") == "June 1, 2026"


def test_splice_readiness_card_marked_and_legacy():
    from estormi_briefing.compose.build_daily_note import (
        READINESS_MARK_END,
        READINESS_MARK_START,
        _readiness_card,
        splice_readiness_card,
    )

    old_card = _readiness_card("Ancienne forme, nuit moyenne.", "fr")
    assert old_card.startswith(READINESS_MARK_START) and old_card.endswith(READINESS_MARK_END)
    body = f"<h1>Briefing</h1><p>intro</p>{old_card}<p>Ma journée…</p>"

    out = splice_readiness_card(body, "Récupération à 82 % : journée pleine possible.", "fr")
    assert out is not None
    assert "82" in out
    assert "Ancienne forme" not in out
    assert out.count(READINESS_MARK_START) == 1
    assert "<p>Ma journée…</p>" in out  # rest of the body untouched

    # Legacy body (built before the markers shipped): structural div match.
    legacy_card = old_card.removeprefix(READINESS_MARK_START).removesuffix(READINESS_MARK_END)
    legacy_body = f"<h1>Briefing</h1>{legacy_card}<p>Ma journée…</p>"
    out2 = splice_readiness_card(legacy_body, "Nouvelle forme.", "fr")
    assert out2 is not None and "Nouvelle forme." in out2 and "Ancienne forme" not in out2

    # No card at all → None (caller falls back to a full run).
    assert splice_readiness_card("<h1>x</h1><p>y</p>", "steer", "fr") is None


def test_render_themes_html_fixes_placeholder_title_and_wrong_year():
    from estormi_briefing.compose.build_daily_note import _render_themes_html

    result = _render_themes_html(
        "THEME: 🤖 IA\nSOURCE: [NateBJones] | [concept] | 2024-06-10\nRésumé du contenu.\n",
        lang="fr",
        date_str="2026-06-12",
    )
    # Bracketed label unwrapped, kind-tag "title" dropped, year clamped.
    assert "<b>NateBJones</b>" in result
    assert "concept" not in result
    assert "2024" not in result and "juin 2026" in result


def test_sane_theme_date_clamps_or_drops():
    from estormi_briefing.compose.build_daily_note import _sane_theme_date

    assert _sane_theme_date("2026-06-10", "2026-06-12") == "2026-06-10"
    assert _sane_theme_date("2024-06-10", "2026-06-12") == "2026-06-10"  # wrong-year slip
    assert _sane_theme_date("2026-01-05", "2026-06-12") == ""  # months off → dropped
    assert _sane_theme_date("2026-06-14", "2026-06-12") == ""  # the future → dropped
    assert _sane_theme_date("n'importe quoi", "2026-06-12") == ""
    assert _sane_theme_date("2024-06-10", "") == "2024-06-10"  # no briefing date → pass through


def test_dont_forget_line_overdue_first_localized_and_capped():
    from estormi_briefing.compose.build_daily_note import _dont_forget_line

    reminders = [
        {"title": "Commander les courses", "when": "18:00", "overdue": False},
        {"title": "Relancer le syndic", "when": "", "overdue": True},
        {"title": "Sans titre", "when": "All day", "overdue": False},
    ]
    out = _dont_forget_line(reminders, "fr")
    # Overdue leads, in red, with the localized badge.
    assert out.index("Relancer le syndic") < out.index("Commander les courses")
    assert "⚠" in out and "en retard" in out
    # Timed reminder keeps its time; all-day one doesn't show "All day".
    assert "Commander les courses — 18:00" in out
    assert "All day" not in out
    assert "À ne pas oublier :" in out
    # Empty input → no line at all.
    assert _dont_forget_line([], "fr") == ""
    # Cap: only the first N items render.
    many = [{"title": f"t{i}", "when": "", "overdue": False} for i in range(10)]
    assert _dont_forget_line(many, "en", cap=3).count("t") >= 3
    assert "t5" not in _dont_forget_line(many, "en", cap=3)


def test_build_note_places_timeline_and_reminders_inside_my_day():
    vision = (
        "OBJECTIVE: La revue de 10h ouvre la journée.\n\n"
        "Un paragraphe d'insight sur la journée qui relie deux événements.\n\n"
        "AROUND: rien."
    )
    strip = '<div class="b-timeline">09:45 Daily</div>'
    body = build_note(
        "2026-06-12",
        3,
        1,
        actions={
            "calendar": [{"when": "09:45", "title": "Daily"}],
            "reminders": [{"title": "Famileo", "when": "", "overdue": False}],
        },
        vision_html=vision,
        lang="fr",
        timeline_html=strip,
    )
    day_section = body.split('<section class="b-day">')[1].split("</section>")[0]
    # Timeline strip before the prose, reminders line after it.
    assert day_section.index("b-timeline") < day_section.index("paragraphe d'insight")
    assert "Famileo" in day_section
    assert "À ne pas oublier" in day_section


def test_build_note_fallback_keeps_timeline_when_prose_fails():
    """When LLM prose composition fails (empty vision_html) the fallback layout
    is taken — the deterministic timeline strip must still render inside
    "My day", not be dropped on the failure path."""
    strip = '<div class="b-timeline">09:45 · Daily</div>'
    body = build_note(
        "2026-06-12",
        3,
        1,
        actions={
            "calendar": [{"when": "09:45", "title": "Daily", "overdue": False}],
            "reminders": [],
        },
        vision_html="",  # prose composition failed → fallback layout
        timeline_html=strip,
    )
    assert "b-timeline" in body
    assert "My day" in body
    # The strip sits inside the My-day section, before the schedule list.
    assert body.index("b-timeline") < body.index("Daily", body.index("b-timeline") + 1)


def test_build_note_fallback_timeline_renders_without_actions():
    """A timeline-only fallback (prose failed, no calendar/reminder actions)
    still opens the My-day section and renders the strip."""
    strip = '<div class="b-timeline">10:00 · Review</div>'
    body = build_note(
        "2026-06-12",
        3,
        1,
        actions={"calendar": [], "reminders": []},
        vision_html="",
        timeline_html=strip,
    )
    assert "My day" in body
    assert "b-timeline" in body and "Review" in body


def test_themes_fold_behind_details_summary():
    body = build_note(
        "2026-06-12",
        3,
        1,
        actions={"calendar": [{"when": "09:45", "title": "Daily"}], "reminders": []},
        vision_html="OBJECTIVE: x.\n\nProse du jour.\n\nAROUND: rien.",
        themes_html="THEME: 🤖 IA\nSOURCE: Chaîne | t | 2026-06-12\nRésumé du contenu.\n",
        lang="fr",
    )
    assert "<details" in body and "</details>" in body
    assert "Veille — aller plus loin" in body
    # The theme content sits INSIDE the details fold.
    fold = body.split("<details")[1]
    assert "Résumé du contenu" in fold


def test_render_themes_html_drops_paraphrase_repeats_and_scaffold_lines():
    from estormi_briefing.compose.build_daily_note import _render_themes_html

    text = (
        "THEME: 🍎 Apple\n"
        "SOURCE: NateBJones | a | 2026-06-10\n"
        "Apple mise sur App Intents et Core ML pour contrôler l'écosystème via "
        "l'OS, en marginalisant les data centers tiers de Nvidia et Google.\n"
        "\n"
        "SOURCE: NateBJones | b | 2026-06-10\n"
        "Apple contrôle l'écosystème via l'OS et App Intents, Core ML "
        "marginalisant les data centers tiers (Nvidia, Google).\n"
        "\n"
        "SOURCE: NateBJones | c | 2026-06-10\n"
        "--- Détails techniques clés (pour NateBJones) : Core ML 8 framework\n"
    )
    out = _render_themes_html(text, lang="fr", date_str="2026-06-12")
    # The paraphrase repeat is dropped (vocabulary containment), the scaffold
    # "---" line never renders.
    assert out.count("App Intents") == 1
    assert "Détails techniques" not in out


def test_render_themes_caps_one_block_per_theme_and_source():
    from estormi_briefing.compose.build_daily_note import _render_themes_html

    text = (
        "THEME: 🍎 Apple\n"
        "SOURCE: NateBJones | a | 2026-06-10\n"
        "Premier bloc retenu sur App Intents.\n"
        "\n"
        "SOURCE: NateBJones | b | 2026-06-10\n"
        "Second bloc, reformulation avec un vocabulaire entièrement différent "
        "pourtant rejetée par le plafond par source et par thème.\n"
        "\n"
        "THEME: 💰 Marchés\n"
        "SOURCE: NateBJones | c | 2026-06-10\n"
        "Même source sous un AUTRE thème : bloc légitime, conservé.\n"
        "\n"
        "Détails techniques clés (pour NateBJones) : scaffolding sans SOURCE.\n"
    )
    out = _render_themes_html(text, lang="fr", date_str="2026-06-12")
    assert "Premier bloc retenu" in out
    assert "Second bloc" not in out  # same (theme, source) → capped
    assert "AUTRE thème" in out  # different theme → kept
    assert "scaffolding sans SOURCE" not in out  # unattributed trailer dropped


def test_fact_fallback_strips_whatsapp_handles_and_urls():
    from estormi_briefing.compose.composer import _fact_fallback

    entry = {
        "label": "whatsapp",
        "title": "",
        "text": (
            "On the road again: [100000000000004@lid]: https://www.airbnb.com/l/XYZ "
            "Hello, J'ai réservé ce gîte pour le mariage d'Alix du 9 au 11"
        ),
        "deadline_iso": "",
    }
    out = _fact_fallback(entry, "French")
    assert "@lid" not in out
    assert "https://" not in out
    assert "(lien)" in out
    assert "gîte" in out


def test_render_themes_drops_episode_that_paraphrases_content():
    """A SOURCE 'title' that restates the block's own summary is dropped —
    the reader must never read the same text twice (Gemma bench, veille)."""
    summary = (
        "Les agents autonomes exécutent des calculs, naviguent sur le web et "
        "manipulent des fichiers pour accomplir des objectifs complexes."
    )
    paraphrase = (
        "Transition vers un modèle de supervision d'agents autonomes capables "
        "d'exécuter des calculs, de naviguer sur le web et de manipuler des "
        "fichiers pour atteindre des objectifs complexes."
    )
    html = _render_themes_html(
        f"THÈME: 🤖 IA\nSOURCE: Nate | {paraphrase} | 2026-06-10\n{summary}\n"
    )
    assert "supervision" not in html  # the episode span is gone
    assert "naviguent sur le web" in html  # the content block stays
    assert "<b>Nate</b>" in html  # the source label stays


def test_render_themes_keeps_real_episode_title():
    html = _render_themes_html(
        "THÈME: 🤖 IA\nSOURCE: Nate | Codex et le nouveau paradigme | 2026-06-10\n"
        "Un résumé du contenu qui parle d'autre chose entièrement.\n"
    )
    assert "Codex et le nouveau paradigme" in html


def test_strip_vision_scaffolding_drops_markdown_hr():
    from estormi_briefing.compose.build_daily_note import _strip_vision_scaffolding

    out = _strip_vision_scaffolding("Premier paragraphe.\n\n---\n\nSecond paragraphe.")
    assert "---" not in out
    assert "Premier paragraphe." in out and "Second paragraphe." in out


def test_briefing_fields_strips_markdown_hr_but_keeps_src():
    from estormi_briefing.compose.build_daily_note import briefing_fields

    vision = (
        "READINESS: Forme correcte.\n\n"
        "OBJECTIVE: Boucler le dossier.\n\n"
        "Le matin avance [src: agenda · 21 Jun].\n\n---\n\nL'après-midi enchaîne."
    )
    fields = briefing_fields(vision)
    assert "---" not in fields["myDay"]
    assert "agenda" in fields["myDay"]  # [src:] kept for the editor round-trip


def test_render_around_drops_world_sourced_bullets():
    from estormi_briefing.compose.build_daily_note import _render_around_html

    html = _render_around_html(
        "- Canicule rouge en France [src: news]\n"
        "- Réponds à Tristan sur le road trip [src: whatsapp · 6 Jun]"
    )
    assert "Canicule" not in html  # world news belongs in "Le monde"
    assert "Tristan" in html  # personal periphery item kept
