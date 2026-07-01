"""On-device briefing text-to-speech via Voxtral (mlx-audio, Metal-accelerated).

The speech counterpart to :mod:`memory_core.llm_local`. The briefing engine hands the
rendered HTML body here and gets back ``.m4a`` audio bytes, synthesized
entirely on the Mac with the Voxtral 4-bit MLX model. Nothing leaves the
machine — the same local-first contract as the rest of the pipeline.

Why the Mac and not the iPhone: the iOS companion is a read-only viewer; all
heavy compute lives here. The Mac writes the audio next to ``briefings/<date>.json``
in the iCloud vault and the companion just plays the file (see
``estormi_ingestion/shared/delivery/vault_sync.py`` and ``apps/estormi-ios``). This replaces the
old on-device sherpa-onnx neural voice that used to ship inside the app.

Model: ``mlx-community/Voxtral-4B-TTS-2603-mlx-4bit`` (~2.5 GB, fits a 16 GB
Mac alongside the briefing LLM). The heavy deps (mlx-audio, soundfile,
mistral-common[audio]) are imported lazily so this module loads on a machine
without them — only :func:`synthesize_isolated` / :func:`download_model` need them.
Apple-Silicon only; mlx has no CUDA/CPU fallback worth shipping.
"""

from __future__ import annotations

import html as _html
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Public surface. Names not listed here (``_``-prefixed helpers) are internal,
# though the test suite reaches a few directly via ``tts_local._name``.
__all__ = [
    "MODEL_REPO",
    "MODEL_DIR_NAME",
    "DEFAULT_TTS_MODEL",
    "TTS_CATALOG",
    "DEFAULT_VOICE",
    "VALID_VOICES",
    "default_voice_for_language",
    "html_to_segments",
    "text_to_segments",
    "model_dir",
    "is_model_downloaded",
    "model_size_bytes",
    "delete_model",
    "download_model",
    "synthesize_isolated",
]

# The single shipped TTS model. A multi-file HF snapshot (config, tokenizer,
# safetensors, the neural-codec weights) — not one GGUF like the briefing LLM,
# so it downloads via ``huggingface_hub.snapshot_download`` rather than the
# byte-range streamer in ``llm_local``.
MODEL_REPO = "mlx-community/Voxtral-4B-TTS-2603-mlx-4bit"
# Pin to an immutable HF commit, not the mutable ``main`` ref, so a force-push /
# mirror compromise can't swap the voice weights under us. ``snapshot_download``
# verifies each file against this revision's manifest. Re-pin on a model bump:
#   curl -s https://huggingface.co/api/models/<repo> | jq -r .sha
MODEL_REVISION = "f98fc91b9cb5adc7dab56102c690458276c14c6a"
MODEL_DIR_NAME = "voxtral-4b-tts"
# Files that, present together, prove the snapshot finished — checked by
# :func:`is_model_downloaded` so a half-pulled directory reads as "not ready".
_SENTINEL_FILES = ("config.json", "tekken.json", "model.safetensors")

# Display + download metadata for the TTS catalog the Officina UI renders —
# the voice counterpart to ``llm_local.MODEL_CATALOG``. One model ships today
# (Voxtral); the dict shape lets the picker list more later without a UI change.
# ``key`` is the stable id the catalog/download/delete endpoints take.
DEFAULT_TTS_MODEL = "voxtral-4b"
TTS_CATALOG: dict[str, dict] = {
    "voxtral-4b": {
        "label": "Voxtral 4B TTS",
        "family": "Mistral",
        "min_ram_gb": 16,
        # ~2.5 GB MLX 4-bit multi-file snapshot — drives the download estimate.
        "expected_bytes": 2_500_000_000,
        "repo": MODEL_REPO,
    },
}

# Voxtral's preset narrator voices (timbre + language). Voxtral takes accent
# AND prosody from the voice preset — narrating a French briefing with the
# English ``neutral_female`` narrator reads as a strong foreign accent. The
# flat default stays English, but callers that know the briefing language
# should resolve their default via :func:`default_voice_for_language`.
DEFAULT_VOICE = "neutral_female"

_LANGUAGE_DEFAULT_VOICE = {
    "fr": "fr_female",
    "es": "es_female",
    "de": "de_female",
    "it": "it_female",
    "pt": "pt_female",
    "nl": "nl_female",
}


def default_voice_for_language(lang_code: str) -> str:
    """The default narrator voice for a briefing-language code."""
    return _LANGUAGE_DEFAULT_VOICE.get((lang_code or "").strip().lower(), DEFAULT_VOICE)


VALID_VOICES = frozenset(
    {
        "fr_female",
        "fr_male",
        "casual_male",
        "casual_female",
        "cheerful_female",
        "neutral_male",
        "neutral_female",
        "es_male",
        "es_female",
        "de_male",
        "de_female",
        "it_male",
        "it_female",
        "pt_male",
        "pt_female",
        "nl_male",
        "nl_female",
        "ar_male",
        "hi_male",
        "hi_female",
    }
)

# Long paragraphs blew up the Metal allocator on a single decode (a 32 GB
# buffer request → crash), so paragraphs over this many characters are split on
# sentence boundaries before synthesis. Also yields a more natural cadence.
_MAX_SEGMENT_CHARS = 320
# ...and very SHORT decodes are where Voxtral hallucinates non-speech (random
# laughter, "hubudubu" filler, clicks at the segment edges): with little text
# the autoregressive duration/end-of-audio prediction is unstable. So adjacent
# short pieces are merged up to this floor before synthesis — no decode is left
# a tiny fragment. The floor is set well above the merge-out-of-laughter minimum
# because EVERY segment boundary is a fresh decode whose first frames carry an
# onset transient (the "sings / babbles at the start of a section" artifact);
# packing fuller segments means fewer boundaries, so the artifact fires less
# often. Capped by _MAX_SEGMENT_CHARS, so a segment is still well inside the
# Metal allocator's budget.
_MIN_SEGMENT_CHARS = 180
# Silence inserted between segments so sections don't run together.
_SEGMENT_GAP_S = 0.28
_SAMPLE_RATE = 24000

# Each segment is a fresh autoregressive decode whose first frames are unstable
# (an onset chirp / "the voice sings" transient at a section start) and whose
# last frames can carry a click. A short raised-cosine fade at each segment edge
# ramps those boundary frames down without touching the body of the speech: the
# fade-in is the longer of the two because the onset is where the audible garble
# lives. Tuned to stay under a single phoneme so no real word is swallowed.
_FADE_IN_S = 0.035
_FADE_OUT_S = 0.018

# Each segment is decoded independently, so their raw amplitudes drift apart →
# audible volume jumps at the joins. We equalise every segment to the run's
# MEDIAN segment RMS (not an absolute target, so the overall loudness the user
# is used to is preserved) before concatenation, with a peak guard so the gain
# never clips.
_PEAK_CEILING = 0.97
# Floor below which a segment is treated as silence and left unscaled (avoids
# amplifying a near-empty decode into noise).
_RMS_FLOOR = 1e-4

# Acoustic flow-matching guidance, set on the loaded model before synthesis.
# NB: generate()'s temperature/top_k/top_p are DEAD params in this model — the
# semantic codes are a pure argmax and the acoustic codes are flow-matched, so
# neither path samples with them. The real lever is cfg_alpha: the model default
# (1.2) lets timbre/level drift mid-utterance ("speaker stepping away from the
# mic"); firmer guidance holds the conditioned voice steadier. n_denoising_steps
# trades a little synthesis time for a cleaner acoustic integration path.
_CFG_ALPHA = 1.8
_DENOISING_STEPS = 12

# Hard cap on audio frames per segment decode. Each frame is 80 ms, so the
# model's own default (4096) allows ~327 s of audio for ONE segment — ~10× more
# than any real segment needs (we cap text at _MAX_SEGMENT_CHARS ≈ 20-30 s ≈
# ~300 frames). That slack is what lets a runaway duration prediction accumulate
# thousands of frames, whose single end-of-run codec decode then asks the Metal
# allocator for a ~32 GB buffer → uncaught C++ abort (SIGABRT) that kills the
# process. Capping at ~60 s (2.5× a real segment) turns that crash into a clean
# truncation: the worst case is a slightly clipped tail, not a dead process.
# The subprocess isolation + retry stays as the net for the residual
# (non-deterministic) case; this just makes it rare.
_MAX_TOKENS = 768  # frames; 768 × 80 ms ≈ 61 s

# Block-level tags force a reader break (a new speakable segment).
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "li", "section", "br", "tr", "hr"}
# Void elements emit no end tag, so they must not move the footer skip counter
# (which balances start/end tags) — counting an unclosed ``<br>`` would leave
# the subtree permanently "open".
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "wbr", "col", "source"}
# Emoji / ornament glyphs the briefing uses for section headers and follow-up
# markers — stripped so the narrator doesn't vocalise or stumble on them. The
# "·" interpunct is NOT here: it separates list items ("climat · énergie ·
# transport"), so blanking it run-runs the words together — it is turned into a
# comma pause by :func:`_normalise_interpuncts` instead.
_ORNAMENTS = "✦✧•↩↪→▸◆◇★☆❧"
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols & pictographs, emoji, supplemental
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicators
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U00002190-\U000021ff"  # arrows
    "]+",
    flags=re.UNICODE,
)
# Lines that are pure footer / provenance, dropped from the spoken text: the
# "Estormi — Briefing …" colophon, the "Sources : …" tally, the "Composé par …"
# credit, and inline source attributions ("Author · "handle" · 5 juin 2026").
_SKIP_LINE_RE = re.compile(
    # The "Estormi —" colophon uses an em/en dash; match that form only so a
    # content sentence merely starting with "Estormi" is never dropped (the
    # footer is also class-skipped in the parser, this is belt-and-suspenders).
    r"^(sources?\s*[:]|composé par|composed by|estormi\s*[—–-]\s)",
    flags=re.IGNORECASE,
)
# A trailing source attribution: an interpunct-separated author/handle run ending
# in a year ("Author · “handle” · 5 juin 2026"). Anchored to the line END and
# barred from crossing a sentence terminator ([^.!?]), so a multi-source bullet
# ("… les marchés corrigent nettement. Le Monde · Reuters · 19 juin 2026") only
# loses the tail, not the sentence body. A pure-attribution line strips to empty
# and is then dropped whole; a real sentence with no attribution never matches.
_ATTRIBUTION_TAIL_RE = re.compile(r"\s*[^.!?]*?·[^.!?]*·[^.!?]*\b\d{4}\b\s*$")


def _strip_attribution_tail(line: str) -> str:
    """Remove a trailing source-attribution tail, keeping any sentence body."""
    return _ATTRIBUTION_TAIL_RE.sub("", line).strip()


def _normalise_interpuncts(line: str) -> str:
    """Turn "·" list separators into comma pauses so items don't run together.

    Runs AFTER the attribution filter (which needs the raw "·" to spot a
    provenance tail). A spaced "a · b" becomes "a, b"; a bare "a·b" gets a
    comma too, and any doubled comma/space from the substitution is collapsed.
    """
    line = line.replace(" · ", ", ").replace("·", ", ")
    return re.sub(r"\s+", " ", line.replace(" ,", ",")).strip()


class _Stripper(HTMLParser):
    """Collapse HTML to plain text, breaking at block boundaries and dropping
    elements whose class marks them as footer chrome."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        # Element-nesting counter for the currently-skipped subtree. While > 0
        # we are inside a footer / script / style element and drop its text. We
        # count EVERY start/end tag inside that subtree (not a fixed whitelist),
        # so the skip ends at the matching close tag regardless of the tag name
        # — a nested block element no longer prematurely zeroes the counter and
        # leaks footer text into the narration.
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            # Already inside a skipped subtree: every nested open tag deepens it,
            # except void elements (no matching end tag would ever close them).
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        classes = dict(attrs).get("class", "") or ""
        if tag in ("script", "style") or "b-footer" in classes or "briefing-footer" in classes:
            self._skip_depth = 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return _html.unescape("".join(self.parts))


def html_to_segments(body: str) -> list[str]:
    """Turn a briefing HTML body into speakable, cleaned paragraphs.

    Strips tags, footer/provenance lines, emojis and ornaments; ensures each
    segment ends on real punctuation so Voxtral lands a natural cadence; and
    splits over-long paragraphs on sentence boundaries to keep each decode
    within the Metal allocator's budget. Pure stdlib — safe to unit-test
    without the heavy ML deps.
    """
    stripper = _Stripper()
    stripper.feed(body)
    raw = _EMOJI_RE.sub("", stripper.text())

    lines: list[str] = []
    for block in raw.split("\n"):
        # Collapse whitespace but keep the "·" separators so the attribution
        # filter ("Author · handle · 5 juin 2026") and the interpunct-to-comma
        # pass below can fire BEFORE ornaments are stripped.
        line = re.sub(r"\s+", " ", block).strip()
        if not line or len(line) < 2:
            continue
        if _SKIP_LINE_RE.match(line):
            continue
        # Strip only a trailing source-attribution tail, keeping any sentence
        # body before it — a pure-attribution line strips to empty and drops.
        line = _strip_attribution_tail(line)
        if not line:
            continue
        line = _normalise_interpuncts(line)
        line = line.translate({ord(c): " " for c in _ORNAMENTS})
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return _pack_segments(lines)


def text_to_segments(text: str) -> list[str]:
    """Turn already-clean prose (e.g. the LLM spoken edition) into segments.

    Unlike :func:`html_to_segments` there is no HTML to parse and no footer /
    attribution to drop — the input is plain narration. We still strip stray
    emojis/ornaments, normalise whitespace, ensure terminal punctuation, and
    split over-long paragraphs to keep each decode within budget.
    """
    text = _EMOJI_RE.sub("", text)
    text = text.translate({ord(c): " " for c in _ORNAMENTS})
    lines: list[str] = []
    for block in re.split(r"\n+", text):
        line = _normalise_interpuncts(re.sub(r"\s+", " ", block).strip())
        if line and len(line) >= 2:
            lines.append(line)
    return _pack_segments(lines)


def _split_long(line: str) -> list[str]:
    """Split a paragraph over the per-decode length cap on sentence ends."""
    if len(line) <= _MAX_SEGMENT_CHARS:
        return [line]
    sentences = re.split(r"(?<=[.!?])\s+", line)
    out: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) + 1 > _MAX_SEGMENT_CHARS and buf:
            out.append(buf.strip())
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        out.append(buf.strip())
    return out


def _pack_segments(lines: list[str]) -> list[str]:
    """Turn cleaned text lines into decode-ready segments of sane length.

    First explode any over-long line on sentence boundaries (the Metal
    allocator cap), then greedily merge adjacent pieces up to
    :data:`_MAX_SEGMENT_CHARS` so no decode is left a tiny fragment — Voxtral
    hallucinates non-speech (laughter, filler, edge clicks) on very short
    inputs. Every emitted segment ends on real punctuation for a natural
    cadence.
    """
    pieces: list[str] = []
    for line in lines:
        pieces.extend(_split_long(line))
    out: list[str] = []
    buf = ""
    for p in pieces:
        if not buf:
            buf = p
        elif len(buf) + 1 + len(p) <= _MAX_SEGMENT_CHARS:
            buf = f"{buf} {p}"
        else:
            out.append(buf)
            buf = p
        if len(buf) >= _MIN_SEGMENT_CHARS:
            out.append(buf)
            buf = ""
    if buf:
        # Fold a short tail back into the previous segment when it fits, rather
        # than leave it as a tiny final decode.
        if out and len(out[-1]) + 1 + len(buf) <= _MAX_SEGMENT_CHARS:
            out[-1] = f"{out[-1]} {buf}"
        else:
            out.append(buf)
    return [s if s[-1] in ".!?:;" else s + "." for s in out]


# --- model location + download -------------------------------------------------


def model_dir() -> Path:
    """On-disk directory holding the Voxtral snapshot.

    Lives under ``${ESTORMI_DATA_DIR}/models`` next to the briefing GGUF so a
    data reset / disk-reclaim treats every model the same way.
    """
    from memory_core import settings  # noqa: PLC0415

    return Path(settings.resolve_data_dir()) / "models" / MODEL_DIR_NAME


def is_model_downloaded() -> bool:
    """True when the snapshot directory holds the sentinel files."""
    d = model_dir()
    return d.is_dir() and all((d / f).exists() for f in _SENTINEL_FILES)


def model_size_bytes() -> int:
    """Total on-disk size of the downloaded snapshot (0 when absent).

    Sums every file under :func:`model_dir` (a multi-file HF snapshot, not one
    GGUF), so the Officina card can show the real footprint like the LLM card.
    """
    d = model_dir()
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def delete_model() -> bool:
    """Remove the downloaded snapshot directory to reclaim disk. Idempotent.

    Returns ``True`` if a directory was removed, ``False`` if nothing was there.
    Unloads the cached model first so synthesis isn't left pointing at unlinked
    files.
    """
    global _model
    d = model_dir()
    if not d.exists():
        return False
    _model = None  # drop the cached mlx-audio model before unlinking its weights
    import shutil  # noqa: PLC0415

    shutil.rmtree(d)
    return True


def download_model() -> str:  # pragma: no cover — network + 2.5 GB snapshot
    """Download the Voxtral snapshot into :func:`model_dir`. Idempotent.

    Synchronous (CDN-bound); call via ``asyncio.to_thread`` from async code.
    Returns the model directory path. Raises on failure.
    """
    d = model_dir()
    if is_model_downloaded():
        return str(d)
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    d.mkdir(parents=True, exist_ok=True)
    _log.info("tts.download.start", repo=MODEL_REPO, dest=str(d))
    snapshot_download(repo_id=MODEL_REPO, revision=MODEL_REVISION, local_dir=str(d))
    if not is_model_downloaded():
        raise RuntimeError("Voxtral snapshot incomplete after download")
    _log.info("tts.download.done", dest=str(d))
    return str(d)


# --- synthesis -----------------------------------------------------------------

_model = None  # cached mlx-audio model, loaded on first synthesis


def _load_model() -> Any:  # pragma: no cover — needs mlx-audio + the downloaded model
    global _model
    if _model is None:
        # Optional Apple-Silicon native dep; may be absent in the CI typecheck env.
        from mlx_audio.tts.utils import (
            load,  # noqa: PLC0415  # pyright: ignore[reportMissingImports]
        )

        if not is_model_downloaded():
            raise RuntimeError("Voxtral TTS model not downloaded — run `make tts-model`")
        _model = load(str(model_dir()))
    return _model


def _normalise_voice(voice: str | None) -> str:
    v = (voice or "").strip()
    return v if v in VALID_VOICES else DEFAULT_VOICE


def _synthesize_wav(segments: list[str], voice: str, wav_path: Path) -> float:  # pragma: no cover
    """Render each segment and write a single PCM16 WAV. Returns audio seconds."""
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415  # pyright: ignore[reportMissingImports]

    model = _load_model()
    # Firm up the acoustic flow-matching so timbre/level hold steady across the
    # read (see _CFG_ALPHA). These are model-instance args, set per synthesis.
    ah_args = model.acoustic_transformer.args
    ah_args.cfg_alpha = _CFG_ALPHA
    ah_args.n_denoising_steps = _DENOISING_STEPS

    sr = _SAMPLE_RATE
    waves: list = []
    for i, seg in enumerate(segments, 1):
        audio = []
        for result in model.generate(text=seg, voice=voice, max_tokens=_MAX_TOKENS):
            sr = int(getattr(result, "sample_rate", sr) or sr)
            audio.append(np.asarray(result.audio, dtype=np.float32))
        if audio:
            seg_wav = np.concatenate(audio)
            waves.append(seg_wav)
            # A segment landing at (or near) the frame cap is a runaway the cap
            # just truncated — log it so a problematic segment is traceable
            # rather than silently clipped.
            if len(seg_wav) / sr >= 0.95 * _MAX_TOKENS * 0.08:
                _log.warning(
                    "tts.segment.capped",
                    index=i,
                    seconds=round(len(seg_wav) / sr, 1),
                    chars=len(seg),
                )
        _log.debug("tts.segment", index=i, total=len(segments))
    if not waves:
        raise RuntimeError("Voxtral produced no audio")

    wav = _equalise_and_join(waves, sr, np)
    sf.write(str(wav_path), wav, sr, subtype="PCM_16")
    return len(wav) / sr


def _edge_fade(w: Any, sr: int, np) -> Any:  # pragma: no cover — needs numpy + a decoded wave
    """Ramp a segment's first/last frames down with a raised-cosine fade.

    Each segment is an independent decode whose onset frames carry the "sings /
    babbles at the start of a section" transient and whose tail can click; a
    short fade at each edge attenuates those boundary frames while leaving the
    body untouched. The fade is clamped to a fraction of the segment so a very
    short wave is never faded edge-to-edge into near-silence.
    """
    n = w.shape[0]
    if n == 0:
        return w
    fin = min(int(sr * _FADE_IN_S), n // 2)
    fout = min(int(sr * _FADE_OUT_S), n // 2)
    if fin > 0:
        # 0→1 raised cosine over the onset.
        ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fin, dtype=np.float32)))
        w[:fin] = w[:fin] * ramp
    if fout > 0:
        # 1→0 raised cosine over the tail.
        ramp = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, fout, dtype=np.float32)))
        w[-fout:] = w[-fout:] * ramp
    return w


def _equalise_and_join(waves: list, sr: int, np) -> Any:  # pragma: no cover
    """Level every segment to the run's median RMS, fade its edges, then join.

    Independent decodes drift in amplitude; matching each to the median segment
    loudness removes the volume jumps at the joins without shifting the overall
    level. A peak guard keeps any boosted segment from clipping. After levelling,
    each segment's edges are faded (see :func:`_edge_fade`) so the boundary
    onset/offset transients don't bleed into the silent gaps.
    """
    rmss = [float(np.sqrt(np.mean(w**2))) for w in waves if w.size]
    target = float(np.median(rmss)) if rmss else 0.0
    gap = np.zeros(int(sr * _SEGMENT_GAP_S), dtype=np.float32)
    chunks: list = []
    for w in waves:
        rms = float(np.sqrt(np.mean(w**2))) if w.size else 0.0
        if target > _RMS_FLOOR and rms > _RMS_FLOOR:
            gain = target / rms
            peak = float(np.max(np.abs(w)))
            if peak * gain > _PEAK_CEILING:
                gain = _PEAK_CEILING / peak
            w = w * gain
        chunks.append(_edge_fade(np.ascontiguousarray(w, dtype=np.float32), sr, np))
        chunks.append(gap)
    return np.concatenate(chunks)


def _wav_to_m4a(wav_path: Path, m4a_path: Path) -> None:  # pragma: no cover — afconvert
    """Encode WAV → AAC/.m4a with macOS' native ``afconvert`` (no ffmpeg)."""
    afconvert = "/usr/bin/afconvert"
    subprocess.run(
        [afconvert, "-f", "m4af", "-d", "aac", str(wav_path), str(m4a_path)],
        check=True,
        capture_output=True,
    )


def _segments_to_m4a(segments: list[str], voice: str) -> bytes:  # pragma: no cover
    """Render speakable segments to AAC/.m4a bytes. Heavy + synchronous."""
    if not segments:
        raise RuntimeError("nothing speakable to synthesize")
    with tempfile.TemporaryDirectory(prefix="estormi-tts-") as tmp:
        wav_path = Path(tmp) / "briefing.wav"
        m4a_path = Path(tmp) / "briefing.m4a"
        secs = _synthesize_wav(segments, voice, wav_path)
        _wav_to_m4a(wav_path, m4a_path)
        _log.info("tts.synth.done", voice=voice, seconds=round(secs, 1), segments=len(segments))
        return m4a_path.read_bytes()


def synthesize_to_m4a(html_body: str, voice: str | None = None) -> bytes:  # pragma: no cover
    """Synthesize a briefing HTML body to AAC/.m4a audio bytes (fallback path).

    Strips the HTML to speakable text directly. Preferred path is
    :func:`synthesize_text_to_m4a` over an LLM "spoken edition"; this is the
    fallback when that rewrite is unavailable. Call via ``asyncio.to_thread``.
    """
    return _segments_to_m4a(html_to_segments(html_body), _normalise_voice(voice))


def synthesize_text_to_m4a(text: str, voice: str | None = None) -> bytes:  # pragma: no cover
    """Synthesize already-clean narration prose to AAC/.m4a bytes.

    Used for the LLM spoken edition of the briefing — prose built to be heard,
    free of the visual scaffolding that makes the on-screen body read awkwardly
    aloud. Call via ``asyncio.to_thread``.
    """
    return _segments_to_m4a(text_to_segments(text), _normalise_voice(voice))


def synthesize_isolated(  # pragma: no cover — spawns a child that loads the model
    content: str,
    voice: str | None = None,
    is_html: bool = False,
    retries: int = 1,
    timeout: float = 2400,
) -> bytes | None:
    """Synthesize in a CHILD process and return the .m4a bytes, or ``None``.

    Voxtral/MLX occasionally aborts the whole process with an uncaught C++
    ``std::runtime_error`` (a runaway duration prediction → a 32 GB Metal
    allocation). That can't be caught in-process, so we run synthesis in a
    subprocess: a crash there is just a non-zero exit we observe here, leaving
    the caller (the briefing worker) alive to ship the briefing without audio.
    Because the failure is non-deterministic, a crashed attempt is retried once.
    """
    voice = _normalise_voice(voice)
    if not content or not content.strip():
        return None
    for attempt in range(retries + 1):
        with tempfile.TemporaryDirectory(prefix="estormi-tts-iso-") as tmp:
            tin = Path(tmp) / "in.txt"
            tout = Path(tmp) / "out.m4a"
            tin.write_text(content, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        __file__,
                        "synth",
                        "html" if is_html else "text",
                        str(tin),
                        str(tout),
                        voice,
                    ],
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                _log.warning("tts.isolated.timeout", attempt=attempt)
                continue
            if proc.returncode == 0 and tout.exists():
                return tout.read_bytes()
            _log.warning(
                "tts.isolated.failed",
                attempt=attempt,
                code=proc.returncode,
                err=proc.stderr.decode(errors="replace")[-300:],
            )
    return None


def _cli() -> int:  # pragma: no cover — subprocess entrypoint (needs the model)
    """Child-process entrypoint: ``tts_local.py synth <text|html> <in> <out> <voice>``."""
    _, _cmd, mode, in_path, out_path, voice = sys.argv
    text = Path(in_path).read_text(encoding="utf-8")
    audio = (
        synthesize_text_to_m4a(text, voice) if mode == "text" else synthesize_to_m4a(text, voice)
    )
    Path(out_path).write_bytes(audio)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
