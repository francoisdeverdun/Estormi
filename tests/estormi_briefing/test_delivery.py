"""Briefing delivery — narration rewrite + audio-attach gating.

Unit tests for :mod:`estormi_briefing.io.delivery`. The spoken-edition rewrite and
the audio attachment are both best-effort: these lock the fall-back contract
(empty/failed rewrite → read the body verbatim) and the gates that skip
synthesis (TTS disabled, or the Voxtral model absent) without ever mutating the
briefing. Heavy TTS work is never invoked — only the seams are exercised.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from estormi_briefing.io import delivery
from estormi_briefing.llm import runtime

pytestmark = pytest.mark.unit

_SENTINEL_DB = object()  # never touched on the early-return paths


def test_collapse_summary_names_the_outage_and_count():
    msg = delivery._collapse_summary(7)
    assert "7 world" in msg
    assert "outage" in msg.lower()


async def test_spoken_edition_returns_stripped_narration():
    with (
        patch("memory_core.tts_local.html_to_segments", return_value=["A sentence."]),
        patch.object(runtime, "_llm_call", AsyncMock(return_value="  Narration text.  ")),
    ):
        out = await delivery._generate_spoken_briefing("<p>A sentence.</p>", "Title", "local", "m")
    assert out == "Narration text."


async def test_spoken_edition_none_when_body_is_empty():
    """No prose to re-voice → skip the LLM entirely and read the body verbatim."""
    with (
        patch("memory_core.tts_local.html_to_segments", return_value=["   "]),
        patch.object(runtime, "_llm_call", AsyncMock()) as llm,
    ):
        out = await delivery._generate_spoken_briefing("<p></p>", "Title", "local", "m")
    assert out is None
    llm.assert_not_called()


async def test_spoken_edition_none_when_llm_raises():
    with (
        patch("memory_core.tts_local.html_to_segments", return_value=["A sentence."]),
        patch.object(runtime, "_llm_call", AsyncMock(side_effect=RuntimeError("CLI down"))),
    ):
        out = await delivery._generate_spoken_briefing("<p>A sentence.</p>", "Title", "local", "m")
    assert out is None


async def test_attach_audio_noop_when_disabled():
    briefing: dict = {"title": "Today"}
    with patch.object(runtime, "_get_setting", AsyncMock(return_value="false")):
        await delivery._maybe_attach_audio(
            _SENTINEL_DB, "2026-06-09", "<p>x</p>", briefing, "l", "m"
        )
    assert "audioPath" not in briefing


async def test_attach_audio_noop_when_model_absent():
    briefing: dict = {"title": "Today"}
    with (
        patch.object(runtime, "_get_setting", AsyncMock(return_value="true")),
        patch("memory_core.tts_local.is_model_downloaded", return_value=False),
    ):
        await delivery._maybe_attach_audio(
            _SENTINEL_DB, "2026-06-09", "<p>x</p>", briefing, "l", "m"
        )
    assert "audioPath" not in briefing


def test_default_voice_follows_briefing_language():
    from memory_core.tts_local import DEFAULT_VOICE, default_voice_for_language

    assert default_voice_for_language("fr") == "fr_female"
    assert default_voice_for_language("FR ") == "fr_female"
    assert default_voice_for_language("en") == DEFAULT_VOICE
    assert default_voice_for_language("") == DEFAULT_VOICE
