"""LLM synthesis — _synthesize_news / _synthesize_themes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from estormi_briefing.compose.prompts import (
    _extract_topics_from_items,
    _news_synthesis_prompt,
    _themes_prompt,
)
from estormi_briefing.compose.synthesis import _synthesize_news, _synthesize_themes

pytestmark = pytest.mark.unit

# ── run_knowledge: news synthesis ────────────────────────────────────────────


def test_news_synthesis_prompt_contains_sources_and_date():
    items = [
        {
            "source_label": "Le Monde",
            "bullets": ["- Ukraine : attaque à Kiev. (Le Monde, 2026-05-15)"],
        },
        {
            "source_label": "Hugo Décrypte",
            "bullets": ["- Ukraine : angle français. (Hugo, 2026-05-15)"],
        },
    ]
    prompt = _news_synthesis_prompt(items, "2026-05-15")

    assert "2026-05-15" in prompt
    assert "Le Monde" in prompt
    assert "Hugo Décrypte" in prompt
    assert "Ukraine" in prompt


def test_news_synthesis_prompt_signal_content_logic():
    """Prompt distinguishes signal vs content sources, requires cross-ref and brevity rules."""
    items = [{"source_label": "Src", "bullets": ["- Info."]}]
    prompt = _news_synthesis_prompt(items, "2026-05-15")

    # Generic signal/content language — no hardcoded source names in instructions
    assert "signal" in prompt.lower()
    assert "content" in prompt.lower()
    # Cross-reference is mandatory
    assert "cross" in prompt.lower() or "merge" in prompt.lower()
    # 6-10 items range
    assert "6" in prompt and "10" in prompt
    # Citation instruction — the model cites item NUMBERS, code attaches the
    # real source/date (so attribution never depends on the model).
    assert "CITATIONS" in prompt
    assert "MANDATORY" in prompt
    assert "number" in prompt.lower()
    # Brevity: mono-source = 1 sentence
    assert "sentence" in prompt.lower()
    # Personal relevance
    assert "France" in prompt or "daily life" in prompt.lower() or "impact" in prompt.lower()
    # "Also worth noting" tail section (folded in from a deleted standalone test).
    assert "worth noting" in prompt.lower()


def test_news_synthesis_prompt_no_hardcoded_source_names():
    """Prompt must not hardcode 'Le Monde' or 'Hugo Décrypte' as named rules."""
    # Single-source call — prompt instructions must be generic
    items = [{"source_label": "Other Source", "bullets": ["- Info."]}]
    prompt = _news_synthesis_prompt(items, "2026-05-15")

    # Source names should appear only in the data section (injected bullets)
    # not baked into the instruction text itself
    instruction_part = prompt.split("Today's sources")[0]
    assert "Le Monde" not in instruction_part
    assert "Hugo Décrypte" not in instruction_part


async def test_synthesize_news_calls_llm_with_news_items():
    items = [
        {
            "axis": "news",
            "source_label": "Le Monde",
            "bullets": ["- Ukraine. (Le Monde, 2026-05-15)"],
        },
        {
            "axis": "news",
            "source_label": "Hugo Décrypte",
            "bullets": ["- Ukraine : angle français. (Hugo, 2026-05-15)"],
        },
    ]

    captured_prompt = {}

    async def fake_llm(prompt, provider, model, **kwargs):
        captured_prompt["text"] = prompt
        # Model cites the input item numbers; code resolves them to sources.
        return "- Ukraine, angle franco-international. [1,2]"

    with patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm):
        result = await _synthesize_news(items, "2026-05-15", "claude-cli", "claude-sonnet-4-6")

    assert "Le Monde" in captured_prompt["text"]
    assert "Hugo Décrypte" in captured_prompt["text"]
    assert "Ukraine" in captured_prompt["text"]
    # Citations resolved to real sources by code; the bullet survives.
    assert "Ukraine" in result
    assert "[SOURCE: Le Monde · Hugo Décrypte | 2026-05-15]" in result


# news/themes synthesize have symmetric error & empty-input contracts —
# parametrize the two together so a divergence in either function trips
# both pairs of cases without doubling the test code.
_SYNTH_FNS = [
    (
        "news",
        lambda items, **kw: _synthesize_news(items, "2026-05-15", "cc", "m"),
        [{"axis": "news", "source_label": "Src", "bullets": ["- Info."]}],
    ),
    (
        "themes",
        lambda items, **kw: _synthesize_themes(items, "2026-05-15", "cc", "m"),
        [{"source_label": "Src", "bullets": ["- Info."]}],
    ),
]


@pytest.mark.parametrize("kind, call, items", _SYNTH_FNS, ids=[s[0] for s in _SYNTH_FNS])
async def test_synthesize_raises_on_llm_failure(kind, call, items):
    with (
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            side_effect=RuntimeError("LLM down"),
        ),
        pytest.raises(RuntimeError, match="LLM down"),
    ):
        await call(items)


@pytest.mark.parametrize("kind, call, items", _SYNTH_FNS, ids=[s[0] for s in _SYNTH_FNS])
async def test_synthesize_returns_empty_for_no_bullets(kind, call, items):
    with patch("estormi_briefing.llm.runtime._llm_call") as mock_llm:
        result = await call([])
    mock_llm.assert_not_called()
    assert result == ""


# ── run_knowledge: theme synthesis ───────────────────────────────────────────


def test_themes_prompt_includes_all_sources_and_date():
    items = [
        {
            "source_label": "Nate B Jones",
            "bullets": ["- [insight] LLM costs drop. (Nate, 2026-05-15)"],
        },
        {
            "source_label": "WelchLabs",
            "bullets": ["- [concept] Backprop explained. (WelchLabs, 2026-05-15)"],
        },
    ]
    prompt = _themes_prompt(items, "2026-05-15")

    assert "2026-05-15" in prompt
    assert "Nate B Jones" in prompt
    assert "WelchLabs" in prompt
    assert "LLM costs drop" in prompt
    assert "Backprop explained" in prompt
    # New format: THEME/SOURCE markers, not HTML
    assert "THEME:" in prompt
    assert "SOURCE:" in prompt


async def test_synthesize_themes_clusters_multi_source():
    items = [
        {
            "source_label": "Nate B Jones",
            "bullets": ["- [insight] OpenAI cuts prices. (Nate, 2026-05-15)"],
        },
        {
            "source_label": "NowTech",
            "bullets": ["- [insight] Claude 4 benchmark. (NowTech, 2026-05-15)"],
        },
        {
            "source_label": "WelchLabs",
            "bullets": ["- [concept] Transformer attention math. (WelchLabs, 2026-05-15)"],
        },
    ]

    llm_output = (
        "THÈME: 🤖 Intelligence Artificielle\n"
        "SOURCE: Nate B Jones | OpenAI price cuts | 2026-05-15\n"
        "OpenAI cuts prices while Claude 4 benchmarks show gains.\n\n"
        "THÈME: 🔬 Sciences\n"
        "SOURCE: WelchLabs | Transformer attention | 2026-05-15\n"
        "Deep dive into transformer attention mathematics."
    )

    captured = {}

    async def fake_llm(prompt, provider, model, **kwargs):
        captured["prompt"] = prompt
        return llm_output

    with patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm):
        result = await _synthesize_themes(items, "2026-05-15", "claude-cli", "claude-sonnet-4-6")

    assert "Nate B Jones" in captured["prompt"]
    assert "WelchLabs" in captured["prompt"]
    assert result == llm_output


# test_synthesize_themes_returns_empty_for_no_bullets and
# test_synthesize_themes_raises_on_llm_failure were folded into the
# parametrized symmetric pair above (kind="themes").

# ── _extract_topics_from_items ────────────────────────────────────────────


def test_extract_topics_from_items_basic():
    items = [
        {
            "source_label": "Hugo Décrypte",
            "bullets": [
                "- Ukraine : frappes russes sur Kharkiv, 200 000 foyers sans électricité.",
                "- Israël-Gaza : Hamas accepte trêve 60 jours.",
            ],
        },
        {
            "source_label": "Le Monde",
            "bullets": ["- OAT 10 ans dépasse 4 % deuxième semaine consécutive."],
        },
    ]
    topics = _extract_topics_from_items(items)
    assert len(topics) == 3
    assert any("Hugo Décrypte" in t for t in topics)
    assert any("Le Monde" in t for t in topics)
    # Each topic is a compact snippet, not the full bullet
    for t in topics:
        assert len(t.split()) <= 12  # "[Source]" + up to 8 words


def test_extract_topics_from_items_empty():
    assert _extract_topics_from_items([]) == []
    assert _extract_topics_from_items([{"source_label": "X", "bullets": []}]) == []


def test_extract_topics_from_items_capped_at_15():
    items = [
        {
            "source_label": "Src",
            "bullets": [f"- Item {i} avec beaucoup de mots." for i in range(30)],
        }
    ]
    topics = _extract_topics_from_items(items)
    assert len(topics) == 15


def test_extract_topics_strips_kind_prefix():
    items = [{"source_label": "Src", "bullets": ["- [insight] Un concept important sur l'IA."]}]
    topics = _extract_topics_from_items(items)
    assert topics
    # The "[insight]" kind tag must not appear verbatim in the snippet
    assert "[insight]" not in topics[0]


def test_extract_topics_fallback_all_items_when_no_news():
    """_extract_topics_from_items works on non-news items for fallback."""
    items = [
        {
            "source_label": "Nate B Jones",
            "bullets": ["- [insight] OpenAI annonce GPT-5 en version commerciale."],
        },
        {
            "source_label": "Hasheur Live",
            "bullets": ["- [insight] Bitcoin dépasse 100k pour la deuxième fois."],
        },
    ]
    topics = _extract_topics_from_items(items)
    assert len(topics) == 2
    assert any("Nate B Jones" in t for t in topics)
    assert any("Hasheur" in t for t in topics)


def test_numbered_news_indexes_each_bullet():
    from estormi_briefing.compose.prompts import _numbered_news

    items = [
        {"source_label": "Le Monde", "bullets": ["- A. (Le Monde, 2026-06-04)"]},
        {"source_label": "Hugo", "bullets": ["- B. (Hugo, 2026-06-03)"]},
    ]
    joined, idx = _numbered_news(items, "2026-06-05")
    assert "[1] [Le Monde]" in joined and "[2] [Hugo]" in joined
    assert idx[1] == {"source": "Le Monde", "date": "2026-06-04"}
    assert idx[2]["date"] == "2026-06-03"


def test_resolve_news_citations_attaches_sources_and_drops_ungrounded():
    from estormi_briefing.compose.prompts import resolve_news_citations

    idx = {
        1: {"source": "Le Monde", "date": "2026-06-04"},
        2: {"source": "Hugo", "date": "2026-06-03"},
    }
    text = (
        "- Merged item. [1,2]\n"
        "- Single item. [1]\n"
        "- Hallucinated item, no citation\n"
        "- Bad citation. [9]\n"
        "— Also worth noting —"
    )
    out = resolve_news_citations(text, idx)
    assert "[SOURCE: Le Monde · Hugo | 2026-06-04]" in out  # most-recent date wins
    assert "[SOURCE: Le Monde | 2026-06-04]" in out
    assert "Hallucinated item" not in out  # no citation → dropped
    assert "Bad citation" not in out  # citation not in index → dropped
    assert "Also worth noting" in out  # non-bullet line kept


def test_fallback_news_from_items_is_real_and_sourced():
    from estormi_briefing.compose.prompts import fallback_news_from_items

    items = [
        {"source_label": "Le Monde", "bullets": ["- [news] Ukraine: raid. (Le Monde, 2026-06-04)"]},
        {"source_label": "Hugo", "bullets": ["- Crédit privé en alerte. (Hugo, 2026-06-03)"]},
    ]
    out = fallback_news_from_items(items, "2026-06-05")
    assert "- Ukraine: raid. [SOURCE: Le Monde | 2026-06-04]" in out
    assert "- Crédit privé en alerte. [SOURCE: Hugo | 2026-06-03]" in out
    assert "[news]" not in out  # kind prefix stripped


async def test_synthesize_news_falls_back_when_model_omits_citations():
    items = [
        {
            "axis": "news",
            "source_label": "Le Monde",
            "bullets": ["- Ukraine. (Le Monde, 2026-05-15)"],
        }
    ]

    async def fake_llm(prompt, provider, model, **kwargs):
        return "- Ukraine, but I forgot to cite a number."  # no [n] → all dropped

    with patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm):
        result = await _synthesize_news(items, "2026-05-15", "local", "ministral3-14b")

    # Falls back to the real input bullet (sourced), not an empty section.
    assert "Ukraine" in result
    assert "[SOURCE: Le Monde | 2026-05-15]" in result


async def test_synthesize_news_reanchors_stale_relative_time_end_to_end():
    """W1 fires inside the real pass chain: a D-1 bullet loses "ce soir"."""
    items = [
        {
            "axis": "news",
            "source_label": "L'Équipe",
            "bullets": ["- Match France-Irak. (L'Équipe, 2026-06-30)"],
        }
    ]

    async def fake_llm(prompt, provider, model, **kwargs):
        # Model writes the source's present tense; code re-anchors it since the
        # citation resolves to 2026-06-30, the day before the briefing.
        return "- Match France-Irak ce soir (23h). [1]"

    with patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm):
        result = await _synthesize_news(items, "2026-07-01", "claude-cli", "opus")

    assert "ce soir" not in result
    assert "le 2026-06-30" in result
    assert "[SOURCE: L'Équipe | 2026-06-30]" in result


def test_fallback_themes_from_items_is_clean_and_sourced():
    from estormi_briefing.compose.prompts import fallback_themes_from_items

    items = [
        {
            "source_label": "Finary",
            "bullets": ["- [insight] Finary Life lancé. (Finary, 2026-06-03)"],
        },
        {
            "source_label": "HasheurLive",
            "bullets": ["- BTC quitte le top 10. (HasheurLive, 2026-06-03)"],
        },
    ]
    out = fallback_themes_from_items(items, "2026-06-05")
    assert "SOURCE: Finary |  | 2026-06-03" in out
    assert "Finary Life lancé." in out
    assert "[insight]" not in out  # kind prefix stripped
    assert "SOURCE: HasheurLive |  | 2026-06-03" in out


async def test_synthesize_themes_falls_back_on_unstructured_output():
    items = [{"source_label": "Finary", "bullets": ["- Finary Life. (Finary, 2026-06-03)"]}]

    async def fake_llm(prompt, provider, model, **kwargs):
        return '**Finance** [Finary] · "truc" · [2026-06-03]'  # markdown, no THEME:/SOURCE:

    with patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm):
        result = await _synthesize_themes(items, "2026-06-05", "local", "ministral3-14b")

    assert "SOURCE: Finary" in result  # canonical, code-built
    assert "Finary Life." in result


async def test_synthesize_themes_trusts_structured_output():
    items = [{"source_label": "Finary", "bullets": ["- Finary Life. (Finary, 2026-06-03)"]}]
    good = "THEME: 💰 Finance\nSOURCE: Finary | Finary Life | 2026-06-03\nA clean summary."

    async def fake_llm(prompt, provider, model, **kwargs):
        return good

    with patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm):
        result = await _synthesize_themes(items, "2026-06-05", "claude-cli", "opus")

    assert result == good  # well-formed output passes through untouched


# ── figure fidelity on summary bullets ────────────────────────────────────────


async def test_figure_check_keeps_clean_originals_and_replaces_flagged():
    """The unflagged originals must NEVER be discarded by the retry — and the
    retry only replaces the flagged bullet."""
    from unittest.mock import AsyncMock, patch

    from estormi_briefing.compose.synthesis import _bullets_with_real_figures

    source = "Le cours est passé sous les 75 000 dollars cette semaine."
    bullets = [
        "- Le cours passe sous 75 000 $. (Ch, 2026-06-11)",  # clean
        "- Le cours chute sous 50 000 $. (Ch, 2026-06-11)",  # phantom figure
    ]
    retry_out = (
        '{"items": [{"kind": "news", "text": "Le cours passe sous les 75 000 $ selon la source."}]}'
    )
    with patch(
        "estormi_briefing.llm.runtime._llm_call", new_callable=AsyncMock, return_value=retry_out
    ) as llm:
        kept = await _bullets_with_real_figures(bullets, source, "PROMPT", "local", "m", "src")
    llm.assert_awaited_once()
    assert bullets[0] in kept  # clean original survives
    assert all("50 000" not in b and "50000" not in b for b in kept)


async def test_figure_check_noop_when_all_supported():
    from unittest.mock import AsyncMock, patch

    from estormi_briefing.compose.synthesis import _bullets_with_real_figures

    source = "récupération à 66% et 75 000 dollars"
    bullets = ["- 66% de récupération.", "- sous 75 000 $."]
    with patch("estormi_briefing.llm.runtime._llm_call", new_callable=AsyncMock) as llm:
        kept = await _bullets_with_real_figures(bullets, source, "P", "local", "m", "src")
    llm.assert_not_awaited()
    assert kept == bullets


async def test_figure_check_drops_when_retry_also_bad():
    from unittest.mock import AsyncMock, patch

    from estormi_briefing.compose.synthesis import _bullets_with_real_figures

    source = "aucun chiffre ici"
    bullets = ["- Une perte de 9 milliards annoncée."]
    retry_out = '{"items": [{"kind": "news", "text": "Une perte de 12 milliards annoncée."}]}'
    with patch(
        "estormi_briefing.llm.runtime._llm_call", new_callable=AsyncMock, return_value=retry_out
    ):
        kept = await _bullets_with_real_figures(bullets, source, "P", "local", "m", "src")
    assert kept == []


def test_cap_impact_lines_keeps_only_the_first_n():
    from estormi_briefing.compose.synthesis import _cap_impact_lines

    text = "\n".join(
        [
            "- A grosse actu → Impact : ton épargne. [SOURCE: x | 2026-06-12]",
            "- B actu → Impact : ton agenda. [SOURCE: y | 2026-06-12]",
            "- C actu → Impact : écho thématique forcé. [SOURCE: z | 2026-06-12]",
            "- D sans impact. [SOURCE: w | 2026-06-12]",
        ]
    )
    out = _cap_impact_lines(text, cap=2).splitlines()
    assert "Impact" in out[0] and "Impact" in out[1]
    assert "Impact" not in out[2]
    assert out[2].endswith("[SOURCE: z | 2026-06-12]")  # the citation survives the cut
    assert out[3] == "- D sans impact. [SOURCE: w | 2026-06-12]"


def test_enforce_news_bounds_backfills_thin_sections():
    from estormi_briefing.compose.synthesis import _enforce_news_bounds

    resolved = (
        "- Grosse actu déjà tenue par la synthèse. [SOURCE: Le Monde | 2026-06-12]\n"
        "- Seconde actu synthétisée. [SOURCE: hugo | 2026-06-12]"
    )
    items = [
        {
            "source_label": "Le Monde",
            "bullets": [
                "Grosse actu déjà tenue par la synthèse.",  # dup of a kept bullet
                *(f"Article distinct numéro {i} sur un sujet propre." for i in range(8)),
            ],
        }
    ]
    out = _enforce_news_bounds(resolved, items, "2026-06-12")
    bullets = [line for line in out.splitlines() if line.startswith("- ")]
    # Backfilled to the floor, the kept synthesis bullets still lead, no dup.
    assert len(bullets) == 6
    assert bullets[0].startswith("- Grosse actu")
    assert sum("Grosse actu" in b for b in bullets) == 1


def test_enforce_news_bounds_trims_bloated_sections():
    from estormi_briefing.compose.synthesis import _enforce_news_bounds

    resolved = "\n".join(f"- Actu {i}. [SOURCE: x | 2026-06-12]" for i in range(14))
    out = _enforce_news_bounds(resolved, [], "2026-06-12")
    assert sum(1 for line in out.splitlines() if line.startswith("- ")) == 10
    assert "- Actu 0." in out and "- Actu 13." not in out


def test_strip_ungrounded_impacts_traces_to_profile():
    import estormi_briefing.llm.runtime as runtime
    from estormi_briefing.compose.synthesis import _strip_ungrounded_impacts

    text = (
        "- Pétrole en hausse → Impact : tes investissements énergie. "
        "[SOURCE: Le Monde | 2026-06-12]\n"
        "- SpaceX entre en bourse → Impact : tes projets spatiaux. "
        "[SOURCE: hugo | 2026-06-12]"
    )
    with patch.object(
        runtime, "user_context", "Je suis ingénieur, je suis mes investissements énergie."
    ):
        out = _strip_ungrounded_impacts(text)
    assert "tes investissements énergie" in out  # grounded → kept
    assert "projets spatiaux" not in out  # invented hook → stripped
    assert "[SOURCE: hugo | 2026-06-12]" in out  # the bullet itself survives

    # No profile at all → every impact is ungroundable → all stripped.
    with patch.object(runtime, "user_context", ""):
        assert "Impact" not in _strip_ungrounded_impacts(text)


# ── deterministic follow-up marking ───────────────────────────────────────────


def test_mark_followups_prefixes_matching_bullet():
    from estormi_briefing.compose.synthesis import mark_followups

    text = (
        "- L'Iran ferme le détroit d'Ormuz, crise énergétique mondiale. [SOURCE: Le Monde | 2026-06-12]\n"
        "- La fusée japonaise H3 a décollé avec succès. [SOURCE: Le Monde | 2026-06-12]"
    )
    topics = "Iran détroit Ormuz frappes américaines; SpaceX introduction en bourse"
    out = mark_followups(text, topics)
    lines = out.splitlines()
    assert lines[0].startswith("- ↩ Follow-up: L'Iran ferme")
    assert "↩" not in lines[1]  # one shared token max — coincidence, not continuity


def test_mark_followups_leaves_marked_and_flagged_bullets():
    from estormi_briefing.compose.synthesis import mark_followups

    text = (
        "- ↩ Follow-up: L'Iran ferme le détroit d'Ormuz. [SOURCE: X | 2026-06-12]\n"
        "- 📅 L'Iran et le détroit d'Ormuz au menu de la réunion. [SOURCE: X | 2026-06-12]"
    )
    out = mark_followups(text, "Iran détroit Ormuz")
    assert out == text


def test_mark_followups_without_topics_is_identity():
    from estormi_briefing.compose.synthesis import mark_followups

    text = "- Une actu quelconque. [SOURCE: X | 2026-06-12]"
    assert mark_followups(text, "") == text


# ── impact floor ──────────────────────────────────────────────────────────────


async def test_impact_floor_adds_grounded_clause_before_marker():
    from estormi_briefing.compose.synthesis import _ensure_impact_floor
    from estormi_briefing.llm import runtime

    text = (
        "- La BCE relève son taux directeur à 2,25 %. [SOURCE: Le Monde | 2026-06-12]\n"
        "- La fusée H3 a décollé. [SOURCE: Le Monde | 2026-06-12]"
    )

    async def fake_llm(prompt, provider, model, **kw):
        return (
            '{"impacts": [{"index": 0, "impact": "Ton crédit immobilier à Clichy se renchérit."}]}'
        )

    with (
        patch.object(runtime, "user_context", "J'habite à Clichy, crédit immobilier en cours."),
        patch.object(runtime, "_llm_call", fake_llm),
    ):
        out = await _ensure_impact_floor(text, "local", "tier", cap=2)
    first = out.splitlines()[0]
    assert "→ Impact: Ton crédit immobilier à Clichy se renchérit." in first
    assert first.index("→ Impact") < first.index("[SOURCE:")


async def test_impact_floor_drops_ungrounded_repair_clause():
    from estormi_briefing.compose.synthesis import _ensure_impact_floor
    from estormi_briefing.llm import runtime

    text = "- La BCE relève son taux. [SOURCE: Le Monde | 2026-06-12]"

    async def fake_llm(prompt, provider, model, **kw):
        return '{"impacts": [{"index": 0, "impact": "Les marchés vont réagir fortement."}]}'

    with (
        patch.object(runtime, "user_context", "J'habite à Clichy."),
        patch.object(runtime, "_llm_call", fake_llm),
    ):
        out = await _ensure_impact_floor(text, "local", "tier", cap=2)
    assert "Impact" not in out  # nothing traces to the profile — honesty rule wins


async def test_impact_floor_noop_when_satisfied_or_no_profile():
    from estormi_briefing.compose.synthesis import _ensure_impact_floor
    from estormi_briefing.llm import runtime

    satisfied = (
        "- A. → Impact: x Clichy. [SOURCE: S | 2026-06-12]\n"
        "- B. → Impact: y Clichy. [SOURCE: S | 2026-06-12]"
    )

    async def boom(prompt, provider, model, **kw):  # must never be called
        raise AssertionError("repair call fired")

    with (
        patch.object(runtime, "user_context", "Clichy"),
        patch.object(runtime, "_llm_call", boom),
    ):
        assert await _ensure_impact_floor(satisfied, "local", "t", cap=2) == satisfied
    with (
        patch.object(runtime, "user_context", ""),
        patch.object(runtime, "_llm_call", boom),
    ):
        bare = "- Une actu. [SOURCE: S | 2026-06-12]"
        assert await _ensure_impact_floor(bare, "local", "t", cap=2) == bare


# ── W1: relative-time re-anchoring ────────────────────────────────────────────


def test_reanchor_relative_time_neutralises_stale_deictic():
    from estormi_briefing.compose.synthesis import _reanchor_relative_time

    # Bullet resolved to the DAY BEFORE the briefing day — "ce soir" is stale.
    text = "- Match France-Irak ce soir (23h). [SOURCE: L'Équipe | 2026-06-30]"
    out = _reanchor_relative_time(text, "2026-07-01")
    assert "ce soir" not in out
    assert "le 2026-06-30" in out
    assert "[SOURCE: L'Équipe | 2026-06-30]" in out  # the marker is untouched


def test_reanchor_relative_time_keeps_same_day_deictic():
    from estormi_briefing.compose.synthesis import _reanchor_relative_time

    # Same-day bullet — the deixis is still correct, leave it alone.
    text = "- Match France-Irak ce soir (23h). [SOURCE: L'Équipe | 2026-07-01]"
    assert _reanchor_relative_time(text, "2026-07-01") == text
    # A future-dated bullet is likewise untouched.
    future = "- Sommet demain à Bruxelles. [SOURCE: Le Monde | 2026-07-02]"
    assert _reanchor_relative_time(future, "2026-07-01") == future
    # A bullet with no resolvable date passes through unchanged.
    undated = "- Rien de daté ce soir."
    assert _reanchor_relative_time(undated, "2026-07-01") == undated


def test_reanchor_relative_time_covers_multiple_deictics():
    from estormi_briefing.compose.synthesis import _reanchor_relative_time

    text = "- Vote hier, résultats aujourd'hui, débat demain. [SOURCE: Le Monde | 2026-06-25]"
    out = _reanchor_relative_time(text, "2026-07-01")
    for stale in ("hier", "aujourd'hui", "demain"):
        assert stale not in out
    assert out.count("le 2026-06-25") == 3


# ── W3: doubled "→ Impact:" dedup ─────────────────────────────────────────────


def test_dedup_impact_clauses_collapses_repeated_clause():
    from estormi_briefing.compose.synthesis import _dedup_impact_clauses

    line = (
        "- La BCE relève son taux. → Impact: ton crédit se renchérit. "
        "→ Impact: ton crédit se renchérit. [SOURCE: Le Monde | 2026-06-12]"
    )
    out = _dedup_impact_clauses(line)
    assert out.count("→ Impact:") == 1
    assert "ton crédit se renchérit" in out
    assert out.endswith("[SOURCE: Le Monde | 2026-06-12]")


def test_dedup_impact_clauses_leaves_single_clause_untouched():
    from estormi_briefing.compose.synthesis import _dedup_impact_clauses

    line = "- La BCE relève son taux. → Impact: ton crédit se renchérit. [SOURCE: x | 2026-06-12]"
    assert _dedup_impact_clauses(line) == line
    # A bare bullet with no impact is likewise untouched.
    bare = "- Une actu sans impact. [SOURCE: x | 2026-06-12]"
    assert _dedup_impact_clauses(bare) == bare


def test_dedup_impact_clauses_keeps_distinct_clauses():
    from estormi_briefing.compose.synthesis import _dedup_impact_clauses

    # Two genuinely different consequences — both kept, single prefix.
    line = (
        "- Actu. → Impact: ton épargne baisse. → Impact: ton loyer grimpe. [SOURCE: x | 2026-06-12]"
    )
    out = _dedup_impact_clauses(line)
    assert out.count("→ Impact:") == 1
    assert "ton épargne baisse" in out and "ton loyer grimpe" in out
