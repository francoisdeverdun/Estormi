"""Per-tier decode profiles + per-stage tier routing (two-quills plumbing)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from estormi_briefing.llm import llm_dispatch
from estormi_briefing.llm.decode_profiles import (
    TWO_QUILLS_ROUTING,
    apply_profile,
    set_stage_routing,
    stage_tier,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _routing_off():
    """Every test starts and ends with routing disabled — module state."""
    set_stage_routing("")
    yield
    set_stage_routing("")


# ── apply_profile ─────────────────────────────────────────────────────────────


def test_gemma_writer_gets_style_directive():
    prompt, max_tokens = apply_profile("gemma4-12b", "writer", "PROMPT", 220)
    assert prompt.startswith("PROMPT")
    assert "STYLE:" in prompt and "clock adjacency" in prompt
    assert max_tokens == 220  # no scale configured — budgets were never the constraint


def test_ministral_writer_gets_insight_directive():
    prompt, _ = apply_profile("ministral3-14b", "writer", "P", 220)
    assert "STYLE:" in prompt and "MEANS for the user" in prompt


def test_unknown_stage_and_unprofiled_stage_are_untouched():
    assert apply_profile("gemma4-12b", "", "P", 220) == ("P", 220)
    assert apply_profile("gemma4-12b", "plan", "P", 1100) == ("P", 1100)
    assert apply_profile("ministral3-14b", "plan", "P", 1100) == ("P", 1100)


# ── routing ───────────────────────────────────────────────────────────────────


def test_routing_off_falls_back_to_selected_tier():
    assert stage_tier("writer", "ministral3-14b") == "ministral3-14b"
    assert stage_tier("lede_alt", "gemma4-12b") == "gemma4-12b"


def test_two_quills_preset_routes_and_crosses_critics():
    set_stage_routing("two-quills")
    assert stage_tier("writer", "x") == "ministral3-14b"
    assert stage_tier("lede", "x") == "gemma4-12b"
    # cross-family critique: critics on the other quill than the writers
    assert stage_tier("critic", "x") != stage_tier("writer", "x")
    assert stage_tier("fact_critic", "x") != stage_tier("writer", "x")
    # the lede tournament spans both families
    assert stage_tier("lede_alt", "x") != stage_tier("lede", "x")


def test_routing_accepts_json_and_survives_garbage():
    assert set_stage_routing('{"writer": "gemma4-12b"}') == {"writer": "gemma4-12b"}
    assert stage_tier("writer", "x") == "gemma4-12b"
    assert set_stage_routing("{not json") == {}  # logs, never raises
    assert stage_tier("writer", "x") == "x"


def test_preset_only_names_catalog_tiers():
    from memory_core.llm_local import MODEL_CATALOG

    assert set(TWO_QUILLS_ROUTING.values()) <= set(MODEL_CATALOG)


# ── dispatch integration ──────────────────────────────────────────────────────


async def test_local_dispatch_passes_routed_tier_and_styled_prompt():
    set_stage_routing("two-quills")
    seen: dict = {}

    async def fake_chat(messages, **kw):
        seen.update(kw, prompt=messages[0]["content"])
        return "ok"

    with patch("memory_core.llm_local.chat_completion", fake_chat):
        out = await llm_dispatch._llm_call_dispatch(
            "PROMPT", "local", "ministral3-14b", max_tokens=220, stage="writer"
        )
    assert out == "ok"
    assert seen["tier"] == "ministral3-14b"

    seen.clear()
    with patch("memory_core.llm_local.chat_completion", fake_chat):
        await llm_dispatch._llm_call_dispatch(
            "PROMPT", "local", "ministral3-14b", max_tokens=700, stage="extractor"
        )
    assert seen["tier"] == "gemma4-12b"  # routed off the selected tier


async def test_local_dispatch_without_stage_keeps_selected_tier():
    seen: dict = {}

    async def fake_chat(messages, **kw):
        seen.update(kw, prompt=messages[0]["content"])
        return "ok"

    with patch("memory_core.llm_local.chat_completion", fake_chat):
        await llm_dispatch._llm_call_dispatch("PROMPT", "local", "gemma4-12b", max_tokens=64)
    assert seen["tier"] == "gemma4-12b"
    assert seen["prompt"] == "PROMPT"  # no stage → no style addendum


def test_preset_upgrades_to_distilled_prose_quill(monkeypatch, tmp_path):
    """When the locally-fused SFT GGUF exists, every Ministral-routed stage
    moves to it — the distillation is a better prose quill, not a third voice."""
    sft = tmp_path / "Ministral-3-14B-Estormi-SFT-Q4_K_M.gguf"
    sft.write_bytes(b"GGUF")
    monkeypatch.setattr("memory_core.llm_local.model_file_path", lambda tier: str(sft))
    set_stage_routing("two-quills")
    assert stage_tier("writer", "x") == "ministral3-14b-estormi"
    assert stage_tier("news_synthesis", "x") == "ministral3-14b-estormi"
    assert stage_tier("lede", "x") == "gemma4-12b"  # the structured quill is untouched
