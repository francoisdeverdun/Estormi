"""Per-tier decode profiles + per-stage tier routing (local provider only).

Every briefing LLM call now names its STAGE ("writer", "lede",
"news_synthesis", …) in its decode-opts dict, and the dispatch layer keys two
decisions on it:

* **profile** — a per-tier, per-stage STYLE directive appended to the prompt.
  The 2026-06-12 bench showed the
  two shipped tiers fail differently — Gemma writes sober and short but stops
  at clock adjacency and skips the correlation markers; Ministral writes rich
  but drifts — so the steering must be per-model, not in the shared template.
* **routing** — which catalog tier runs the stage. This is the two-quills
  mode: route each stage to the model that wins it, and route the critics to
  the OTHER family than the writers (cross-family judging counters the
  self-preference bias documented for LLM judges). Empty by default —
  single-model behavior, byte-identical prompts.

The routing is run-scoped state set by the orchestrator from the
``briefing_stage_routing`` setting: ``""`` (off), ``"two-quills"`` (the
built-in preset below), or a JSON object ``{"stage": "tier"}`` for manual
experiments. Unknown tiers are ignored at dispatch (the call falls back to
the selected tier), so a stale setting can never brick the run.
"""

from __future__ import annotations

import json

import structlog

log = structlog.get_logger()

# ── per-tier profiles ─────────────────────────────────────────────────────────

# Style directives are writer instructions (template language: English); the
# output language is already pinned by each prompt's own directive.
_GEMMA_INSIGHT = (
    "Write 2-3 full, connected sentences: state the stake or the concrete "
    "consequence that links the facts — never their mere clock adjacency "
    "('X flows into Y with no pause' alone is not insight)."
)

# Both quills narrate adjacency when left alone — the two-quills validation
# run (2026-06-12) shipped three Ministral paragraphs that each restated
# "X follows Y without pause". The steer is the same for both: say what the
# sequence MEANS for the user.
_WRITER_INSIGHT = (
    "State what the sequence MEANS for the user — what to prepare, decide or "
    "carry from one block to the next; one concrete consequence per "
    "paragraph. Mention an event's '(tentative)' status when its row carries "
    "it. Never write three variations of 'X flows into Y without pause'."
)

TIER_PROFILES: dict[str, dict] = {
    "gemma4-12b": {
        # Bench evidence (2026-06-12): vision half Ministral's length, world
        # section shipped with zero impact/follow-up markers, narration was
        # pure adjacency. No max_tokens scale — the budgets were never the
        # binding constraint; the style was.
        "style": {
            "writer": f"{_GEMMA_INSIGHT} {_WRITER_INSIGHT}",
            "cohesion": (
                "Keep each paragraph 2-3 full sentences; preserve the insight "
                "linking the facts, not just their sequence."
            ),
            "vision": _GEMMA_INSIGHT,
            "news_synthesis": (
                "When an item directly touches the PERSONAL CONTEXT, append "
                "its '→ Impact:' clause; when a topic was in LAST BRIEFING "
                "TOPICS, prefix the bullet with '↩ Follow-up:'. Do not skip "
                "these markers."
            ),
        },
    },
    "ministral3-14b": {
        "style": {
            "writer": _WRITER_INSIGHT,
            "cohesion": (
                "Vary the connective tissue: at most ONE paragraph may state "
                "a back-to-back chain; the others must carry a consequence "
                "or a preparation, not another 'sans pause'."
            ),
        },
    },
}


def apply_profile(
    tier: str, stage: str, prompt: str, max_tokens: int | None
) -> tuple[str, int | None]:
    """Apply ``tier``'s profile for ``stage`` to a local call's prompt/budget."""
    profile = TIER_PROFILES.get(tier)
    if not profile or not stage:
        return prompt, max_tokens
    style = (profile.get("style") or {}).get(stage)
    if style:
        prompt = f"{prompt}\n\nSTYLE: {style}"
    return prompt, max_tokens


# ── per-stage tier routing (two quills) ───────────────────────────────────────

# The built-in preset, from the 2026-06-12 four-model bench: structured /
# sobriety-first stages go to Gemma, prose / correlation stages to Ministral,
# and the critics to the other family than the writers. Stages are grouped so
# consecutive calls share a model — a tier swap costs ~15-30s on a 16 GB Mac
# (the two GGUFs cannot co-reside) and the run makes ~15 calls.
TWO_QUILLS_ROUTING: dict[str, str] = {
    # extraction / JSON / sober statement → Gemma 4 12B
    "summary": "gemma4-12b",
    "consolidation": "gemma4-12b",
    "extractor": "gemma4-12b",
    "plan": "gemma4-12b",
    "readiness": "gemma4-12b",
    "readiness_repair": "gemma4-12b",
    # tutoiement enforcement: a faithful address-only rewrite — the sober,
    # instruction-following quill, already resident after readiness_repair.
    "voice_repair": "gemma4-12b",
    "lede": "gemma4-12b",
    "profile": "gemma4-12b",
    # prose / correlation → Ministral 3 14B
    "news_synthesis": "ministral3-14b",
    "impact_repair": "ministral3-14b",
    "themes": "ministral3-14b",
    "writer": "ministral3-14b",
    "cohesion": "ministral3-14b",
    "narration": "ministral3-14b",
    # the lede tournament's challenger candidate — the other quill from "lede"
    "lede_alt": "ministral3-14b",
    "judge": "ministral3-14b",
    # cross-family critique: the writers above are Ministral, so the critics
    # are Gemma — a judge from the writer's own family over-trusts its voice
    # (self-preference bias), and the bench's one shipped hallucination was
    # exactly a self-approved line.
    "critic": "gemma4-12b",
    "fact_critic": "gemma4-12b",
}

# Run-scoped routing map — set by the orchestrator at the top of each run.
_stage_routing: dict[str, str] = {}


# The locally-distilled prose quill (QLoRA fusion of Ministral on
# cloud-briefing style). When its GGUF is installed, the two-quills preset
# upgrades every Ministral-routed stage to it — the distillation IS a better
# prose quill, not a third voice.
_SFT_PROSE_TIER = "ministral3-14b-estormi"


def _preset_routing() -> dict[str, str]:
    routing = dict(TWO_QUILLS_ROUTING)
    try:
        from pathlib import Path  # noqa: PLC0415

        from memory_core.llm_local import model_file_path  # noqa: PLC0415

        if Path(model_file_path(_SFT_PROSE_TIER)).exists():
            routing = {
                k: (_SFT_PROSE_TIER if v == "ministral3-14b" else v) for k, v in routing.items()
            }
            log.info("stage routing: distilled prose quill installed — preset upgraded")
    except Exception:  # noqa: BLE001 — the stock preset always works
        pass
    return routing


def set_stage_routing(spec: str) -> dict[str, str]:
    """Parse + activate the ``briefing_stage_routing`` setting for this run.

    ``""``/unset → off; ``"two-quills"`` → the built-in preset (upgraded to
    the distilled prose quill when installed); anything else must be a JSON
    object mapping stage → tier (invalid JSON logs and turns routing off — a
    bad setting must never brick the briefing).
    """
    global _stage_routing
    spec = (spec or "").strip()
    if not spec:
        _stage_routing = {}
    elif spec.lower() in ("two-quills", "two_quills", "twoquills"):
        _stage_routing = _preset_routing()
    else:
        try:
            parsed = json.loads(spec)
            if not isinstance(parsed, dict):
                raise ValueError("expected a dict")
            _stage_routing = {str(k): str(v) for k, v in parsed.items()}
        except Exception:  # noqa: BLE001 — a bad setting never bricks the run
            log.warning("stage routing: unparseable spec %.80r — routing off", spec)
            _stage_routing = {}
    if _stage_routing:
        log.info("stage routing active: %d stage(s) mapped", len(_stage_routing))
    return _stage_routing


def stage_tier(stage: str, fallback: str) -> str:
    """The tier that should run ``stage`` — the routed one, else ``fallback``.

    ``lede_alt`` exists only in routing: unrouted, it resolves to the same
    tier as ``lede`` would (the fallback), which collapses the two-family
    lede tournament back to plain best-of-N.
    """
    return _stage_routing.get(stage) or fallback
