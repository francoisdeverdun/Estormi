"""Unit tests for memory_core/tts_local.py — the briefing TTS engine.

Cover the pure, dependency-free parts (HTML→speech-text cleaning, voice
validation, model-path resolution). Actual synthesis needs the mlx-audio stack
and a 2.5 GB model, so it is not exercised here.
"""

from __future__ import annotations

import pytest

from memory_core import tts_local

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# html_to_segments — cleaning
# ---------------------------------------------------------------------------


def test_strips_emojis_and_ornaments() -> None:
    segs = tts_local.html_to_segments("<h2>📅 Ma journée</h2><p>✦ Un point ↩ suivi.</p>")
    joined = " ".join(segs)
    assert "📅" not in joined and "✦" not in joined and "↩" not in joined
    assert "Ma journée" in joined
    assert "Un point" in joined


def test_drops_footer_and_provenance_lines() -> None:
    html = (
        "<p>Le contenu réel du briefing.</p>"
        '<p class="b-footer">Estormi — Briefing du 6 juin 2026 à 20:22<br>'
        "Sources : 8 canaux<br>Composé par claude-cli/opus.</p>"
    )
    segs = tts_local.html_to_segments(html)
    joined = " ".join(segs)
    assert "contenu réel" in joined
    assert "Composé par" not in joined
    assert "Sources" not in joined
    assert "Estormi —" not in joined


def test_footer_with_nested_block_does_not_leak() -> None:
    # A nested block element inside the footer used to prematurely zero the
    # skip-depth counter, leaking the rest of the footer into the narration.
    # The balanced start/end counter must keep the WHOLE footer subtree skipped.
    html = (
        "<p>Real briefing content.</p>"
        '<div class="briefing-footer">'
        "<p>Sources tally line one.</p>"
        "<div>Composed by claude-cli.</div>"
        "<p>Composé par opus.</p>"
        "</div>"
        "<p>Trailing real content after footer.</p>"
    )
    segs = tts_local.html_to_segments(html)
    joined = " ".join(segs)
    assert "Real briefing content" in joined
    assert "Trailing real content" in joined
    assert "Sources tally" not in joined
    assert "Composed by" not in joined
    assert "Composé par" not in joined


def test_footer_in_unbalanced_tag_does_not_swallow_rest() -> None:
    # The footer element is a tag NOT in the old decrement whitelist (<ul>).
    # The balanced counter still closes the skip at its matching </ul>, so the
    # following real content is not swallowed.
    html = '<ul class="b-footer"><li>Sources: 8 channels</li></ul><p>Content that must survive.</p>'
    segs = tts_local.html_to_segments(html)
    joined = " ".join(segs)
    assert "Content that must survive" in joined
    assert "Sources" not in joined


def test_drops_source_attribution_lines() -> None:
    html = "<p>Une analyse intéressante.</p><p>NateBJones · “NateBJones” · 5 juin 2026</p>"
    segs = tts_local.html_to_segments(html)
    joined = " ".join(segs)
    assert "analyse intéressante" in joined
    assert "NateBJones" not in joined


def test_multi_source_bullet_keeps_the_sentence_body() -> None:
    # A multi-source inline bullet ("<body>. Source A · Source B · <year>") used
    # to match the attribution filter as a whole and vanish from the audio. Only
    # the trailing provenance tail should be dropped; the sentence must survive.
    html = (
        "<li>La BCE relève ses taux et les marchés corrigent nettement. "
        "Le Monde · Reuters · 19 juin 2026</li>"
    )
    segs = tts_local.html_to_segments(html)
    joined = " ".join(segs)
    assert "les marchés corrigent nettement" in joined
    assert "Reuters" not in joined
    assert "2026" not in joined


def test_interpunct_list_narrates_with_commas() -> None:
    # The "·" separator used to be blanked with the other ornaments, running the
    # items together ("climat énergie transport"). It now becomes a comma pause.
    html = "<p>Trois sujets à suivre : climat · énergie · transport de demain.</p>"
    joined = " ".join(tts_local.html_to_segments(html))
    assert "climat, énergie, transport" in joined
    assert " · " not in joined and "·" not in joined

    # Same on the already-clean spoken-edition path.
    joined_text = " ".join(tts_local.text_to_segments("climat · énergie · transport"))
    assert "climat, énergie, transport" in joined_text


def test_adds_terminal_punctuation() -> None:
    segs = tts_local.html_to_segments("<h1>Briefing du jour</h1>")
    assert segs == ["Briefing du jour."]


def test_splits_overlong_paragraphs() -> None:
    sentence = "Ceci est une phrase de test assez longue pour compter. "
    body = f"<p>{sentence * 12}</p>"  # well over the per-decode cap
    segs = tts_local.html_to_segments(body)
    assert len(segs) > 1
    assert all(len(s) <= tts_local._MAX_SEGMENT_CHARS + 1 for s in segs)


def test_empty_body_yields_no_segments() -> None:
    assert tts_local.html_to_segments("") == []
    assert tts_local.html_to_segments("<p></p>") == []


# ---------------------------------------------------------------------------
# text_to_segments — the LLM spoken-edition path
# ---------------------------------------------------------------------------


def test_text_to_segments_merges_short_paragraphs() -> None:
    # Two short paragraphs are merged into one decode: Voxtral hallucinates
    # non-speech (laughter, filler) on tiny inputs, so packing keeps each
    # segment above the floor. Content and order are preserved.
    text = "Première phrase parlée.\n\nDeuxième paragraphe, lui aussi parlé."
    segs = tts_local.text_to_segments(text)
    assert len(segs) == 1
    assert segs[0] == "Première phrase parlée. Deuxième paragraphe, lui aussi parlé."


def test_pack_segments_keeps_each_below_cap_and_above_floor() -> None:
    # A run of short lines packs into segments that respect the length cap;
    # only the final (leftover) segment may fall below the floor.
    lines = ["Phrase numéro un assez courte."] * 20
    segs = tts_local._pack_segments(lines)
    assert all(len(s) <= tts_local._MAX_SEGMENT_CHARS + 1 for s in segs)
    assert all(len(s) >= tts_local._MIN_SEGMENT_CHARS for s in segs[:-1])


def test_text_to_segments_adds_punctuation_and_strips_emojis() -> None:
    segs = tts_local.text_to_segments("Bonjour 👋 et bienvenue")
    assert segs == ["Bonjour et bienvenue."]


def test_text_to_segments_respects_length_cap() -> None:
    long = "Une phrase de test relativement longue à lire. " * 12
    segs = tts_local.text_to_segments(long)
    assert all(len(s) <= tts_local._MAX_SEGMENT_CHARS + 1 for s in segs)


def test_pack_merges_to_the_higher_floor_to_cut_onset_count() -> None:
    # Fuller segments mean fewer decode boundaries, and every boundary is a
    # fresh-decode onset where Voxtral chirps/babbles. A run of short lines must
    # therefore pack up to the (raised) floor, not stop at the old 100-char one.
    lines = ["Une ligne assez courte de test."] * 12
    segs = tts_local._pack_segments(lines)
    assert all(len(s) >= tts_local._MIN_SEGMENT_CHARS for s in segs[:-1])
    assert tts_local._MIN_SEGMENT_CHARS >= 160


# ---------------------------------------------------------------------------
# _edge_fade — onset/offset transient softening
# ---------------------------------------------------------------------------


def test_edge_fade_ramps_boundaries_and_preserves_body() -> None:
    np = pytest.importorskip("numpy")
    sr = tts_local._SAMPLE_RATE
    w = np.ones(sr, dtype=np.float32)  # 1 s of full-scale signal
    out = tts_local._edge_fade(w.copy(), sr, np)
    fin = int(sr * tts_local._FADE_IN_S)
    fout = int(sr * tts_local._FADE_OUT_S)
    # The first/last samples are ramped to (near) silence...
    assert out[0] < 0.05
    assert out[-1] < 0.05
    # ...the onset rises monotonically over the fade-in window...
    assert np.all(np.diff(out[:fin]) >= -1e-6)
    # ...and the body between the fades keeps full amplitude (no word swallowed).
    assert np.allclose(out[fin : len(out) - fout], 1.0)


def test_edge_fade_handles_empty_and_tiny_waves() -> None:
    np = pytest.importorskip("numpy")
    sr = tts_local._SAMPLE_RATE
    assert tts_local._edge_fade(np.zeros(0, dtype=np.float32), sr, np).shape[0] == 0
    # A wave shorter than the fade windows must not over-read or blow up.
    tiny = np.ones(4, dtype=np.float32)
    out = tts_local._edge_fade(tiny.copy(), sr, np)
    assert out.shape[0] == 4
    assert out[0] <= tiny[0]


# ---------------------------------------------------------------------------
# voice validation
# ---------------------------------------------------------------------------


def test_default_voice_is_english_neutral() -> None:
    # The briefing default language is English, so the default narrator voice is
    # the language-neutral English ``neutral_female`` preset, not a French voice.
    assert tts_local.DEFAULT_VOICE == "neutral_female"
    assert tts_local.DEFAULT_VOICE in tts_local.VALID_VOICES


@pytest.mark.parametrize("bad", ["", "  ", "nonsense", "EN_MALE", None])
def test_normalise_voice_falls_back(bad) -> None:
    assert tts_local._normalise_voice(bad) == tts_local.DEFAULT_VOICE


def test_normalise_voice_keeps_valid() -> None:
    assert tts_local._normalise_voice("fr_male") == "fr_male"


# ---------------------------------------------------------------------------
# model path / download state
# ---------------------------------------------------------------------------


def test_model_dir_honours_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    d = tts_local.model_dir()
    assert d == tmp_path / "models" / tts_local.MODEL_DIR_NAME


def test_is_model_downloaded_requires_sentinels(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    assert tts_local.is_model_downloaded() is False

    d = tts_local.model_dir()
    d.mkdir(parents=True)
    # A partial snapshot (missing files) still reads as "not ready".
    (d / "config.json").write_text("{}")
    assert tts_local.is_model_downloaded() is False

    for f in tts_local._SENTINEL_FILES:
        (d / f).write_text("x")
    assert tts_local.is_model_downloaded() is True
