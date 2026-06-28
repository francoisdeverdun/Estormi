"""Briefing delivery — spoken-edition narration + audio attachment.

After ``run_briefing`` composes the briefing it hands the body here to (best
effort) synthesize a spoken edition and attach ``briefings/<date>.m4a`` to the
vault. Everything is gated and failure-tolerant: a missing TTS model or a
synthesis crash never breaks the run, it just ships the briefing without audio.

The run-scoped LLM call (``_llm_call``) and the settings reader
(``_get_setting``) are looked up on the ``runtime`` module at call time so the
orchestrator's per-run metric recorder and test patches still apply.
"""

from __future__ import annotations

import asyncio

import structlog

from estormi_briefing.compose.prompts import narration_prompt
from estormi_briefing.llm import runtime
from estormi_ingestion.shared.delivery.vault_sync import write_briefing_audio as _vault_write_audio

log = structlog.get_logger()


def _collapse_summary(world_chunk_count: int) -> str:
    """Status line for a run where world material existed but every LLM call
    produced nothing — the signature of a transient LLM/CLI outage."""
    return (
        f"Every LLM summarization failed across {world_chunk_count} world "
        "chunk(s) — likely a transient LLM/CLI outage. Kept the previous "
        "briefing rather than overwriting it with an empty one."
    )


async def _generate_spoken_briefing(body: str, title: str, provider: str, model: str) -> str | None:
    """LLM "spoken edition" of the briefing — same facts, built to be heard.

    The on-screen body reads awkwardly aloud (percentages, clock forms, deltas,
    section headers, citations), so before synthesis we have the LLM re-voice it
    as flowing narration. ``title`` anchors the opening line so the model never
    invents a greeting or guesses the weekday. Returns the narration text, or
    ``None`` if the rewrite is empty/fails (the caller then reads the stripped
    body verbatim). Uses the same provider/model as the briefing writer — so for
    the local provider this MUST run before the LLM is unloaded to free the GPU
    for the TTS model.
    """
    from memory_core import tts_local  # noqa: PLC0415

    # Feed the LLM the already-cleaned text (footer/emoji/attribution stripped),
    # not raw HTML — it should rewrite prose, not markup.
    clean = "\n".join(tts_local.html_to_segments(body))
    if not clean.strip():
        return None
    try:
        # Local decode options (inert for claude-cli): the narration re-voices
        # the WHOLE briefing, so it needs the run's largest reply budget and
        # the wall-clock to match (~2500 tokens outruns the 300s default); a
        # low temperature keeps it fluent without drifting from the facts.
        spoken = await runtime._llm_call(
            narration_prompt(clean, title),
            provider,
            model,
            max_tokens=2500,
            temperature=0.2,
            timeout=600.0,
            stage="narration",
        )
    except Exception:  # noqa: BLE001 — optional; fall back to reading the body
        log.exception("TTS: spoken-edition rewrite failed; will read the body verbatim")
        return None
    spoken = (spoken or "").strip()
    return spoken or None


async def _maybe_attach_audio(
    db, today: str, body: str, briefing: dict, provider: str, model: str
) -> None:
    """Synthesize the briefing to speech and attach it to ``briefing``.

    Gated on ``briefing_tts_enabled`` (default on) and the Voxtral model being
    present. First asks the LLM for a spoken edition of the briefing (built for
    the ear), then synthesizes that with Voxtral and writes
    ``briefings/<today>.m4a`` to the vault, setting ``briefing["audioPath"]`` so
    the iOS companion shows its player only when audio exists. Best-effort: any
    failure logs and returns, leaving the briefing text untouched. Synthesis is
    heavy (~real-time) so it runs in a worker thread off the event loop.
    """
    enabled = (await runtime._get_setting(db, "briefing_tts_enabled", "true")).strip().lower()
    if enabled not in ("true", "1", "yes", "on"):
        return
    try:
        from memory_core import tts_local  # noqa: PLC0415
    except Exception:  # pragma: no cover — mlx stack absent (non-Mac / CI)
        log.warning("TTS: tts_local unavailable; skipping narration")
        return
    if not tts_local.is_model_downloaded():
        log.info("TTS: Voxtral model not downloaded; skipping narration (run `make tts-model`)")
        return

    # 1. Spoken edition — must happen before the LLM is unloaded below. The
    # briefing title anchors the opening so the rewrite never invents a weekday.
    spoken = await _generate_spoken_briefing(
        body, str(briefing.get("title") or ""), provider, model
    )

    # 2. Free the briefing LLM before loading the TTS model — both want the Metal
    # GPU and a 16 GB Mac can't hold Ministral 14B and Voxtral at once. No-op
    # when the briefing used the Claude-CLI provider (nothing was loaded).
    try:
        from memory_core import llm_local  # noqa: PLC0415

        await llm_local.unload()
    except Exception:  # noqa: BLE001 — unload is best-effort
        pass

    # 3. Synthesize in a CHILD process: Voxtral/MLX can abort the whole process
    # with an uncaught C++ error, and that must not kill the briefing worker
    # before the JSON is pushed. The spoken edition when we have it, else the
    # stripped body. Returns None on a (retried) crash → ship without audio.
    # No voice picked (unset OR stored as "" — the selector's "auto" value) →
    # derive from the briefing language: Voxtral's accent comes from the voice
    # preset, and the English narrator reading French sounds like a strong
    # foreign accent.
    lang_code = (await runtime._get_setting(db, "briefing_language", "fr")).strip().lower()
    voice = (await runtime._get_setting(db, "briefing_tts_voice", "")).strip() or (
        tts_local.default_voice_for_language(lang_code)
    )
    content, is_html = (spoken, False) if spoken else (body, True)
    try:
        audio = await asyncio.to_thread(tts_local.synthesize_isolated, content, voice, is_html)
    except Exception:  # noqa: BLE001 — narration is optional; never break a run
        log.exception("TTS: synthesis failed; briefing shipped without audio")
        return
    if not audio:
        log.warning("TTS: synthesis produced no audio (model crash?); shipped without audio")
        return
    if _vault_write_audio(today, audio):
        briefing["audioPath"] = f"briefings/{today}.m4a"
        log.info(
            "TTS: attached %s narration (%d KB) to briefing %s",
            "spoken-edition" if spoken else "verbatim",
            len(audio) // 1024,
            today,
        )
