"""GET /api/knowledge/llm-models — provider-driven model dropdown options."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestKnowledgeLlmModels:
    async def test_claude_cli_lists_aliases(self, client):
        resp = await client.get("/api/knowledge/llm-models?provider=claude-cli")
        assert resp.status_code == 200
        body = resp.json()
        assert body["setting_key"] == "knowledge_llm_model"
        values = [m["value"] for m in body["models"]]
        assert values == ["claude-fable-5", "opus", "sonnet"]

    async def test_local_reports_tier_setting_key(self, client):
        resp = await client.get("/api/knowledge/llm-models?provider=local")
        assert resp.status_code == 200
        body = resp.json()
        # The local provider loads a GGUF tier, not a free-text model name.
        assert body["setting_key"] == "briefing_model_tier"
        assert isinstance(body["models"], list)
        assert body["current"]  # always a tier (chosen or default)

    async def test_unknown_provider_keeps_saved_value(self, client):
        resp = await client.get("/api/knowledge/llm-models?provider=ollama")
        assert resp.status_code == 200
        body = resp.json()
        assert body["setting_key"] == "knowledge_llm_model"
        assert body["models"] == []  # no enumerable catalogue
        assert body["current"]  # the saved value is still surfaced
