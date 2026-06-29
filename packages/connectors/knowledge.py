"""Knowledge (world) — external YouTube transcripts + RSS articles.

Fetches the sources configured on the Briefing page and stores them as raw
``world``-corpus chunks (no LLM). The Briefing engine then reads these back
from the DB at composition time instead of fetching transcripts itself.
"""

from __future__ import annotations

from .base import ConnectorSpec, ScriptConnector, registry


@registry.register
class KnowledgeConnector(ScriptConnector):
    spec = ConnectorSpec(
        name="knowledge",
        title="External knowledge",
        description="Ingests external YouTube transcripts + RSS articles as world-corpus memory.",
        dag_stage=True,
        dag_order=11,
        default_stage=True,
        uses_watermark=True,
        # First-run window: how far back the very first ingest reaches. The
        # Manage modal offers 1W/2W/1M/3M/ALL; default 1 week so a fresh
        # install doesn't pull months of transcripts/articles on day one.
        depth_window_env="KNOWLEDGE_DAYS_WINDOW",
        default_depth="1w",
    )
    module = "estormi_ingestion.knowledge.ingest_world"
