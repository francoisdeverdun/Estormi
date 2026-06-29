"""World-corpus read + reassembly for the briefing.

The Briefing engine no longer fetches transcripts/articles itself — the
``knowledge`` ingestion stage (``ingest_world.py``) stores them as
``corpus=world`` and the briefing reads them back. This module recovers the
per-source key from a stored chunk's ``source_id``, reassembles sliding-window
splits back into whole items, and exposes the two world-corpus fetches the
orchestrator drives (today's news + the past-coverage follow-up).
"""

from __future__ import annotations

import os
from datetime import date

from estormi_briefing.io.mcp_io import _fetch_around_mcp, _search_mcp_memory

# How many world chunks the past-follow-up RAG search may surface for the
# day-vision (developing-story context drawn from the whole world corpus).
_WORLD_FOLLOWUP_LIMIT = int(os.getenv("BRIEFING_WORLD_FOLLOWUP_LIMIT", "8"))

# Half-width (in days) of the world-corpus window the briefing reads as "today's
# news". 0 = strictly the briefing day; the default of 1 tolerates timezone
# skew between ingest (UTC) and the local briefing day.
_WORLD_WINDOW_DAYS = int(os.getenv("BRIEFING_WORLD_WINDOW_DAYS", "1"))


def _parse_world_source_key(source_id: str) -> str:
    """Recover the source key from a world chunk's ``source_id``.

    ``ingest_world`` stores world chunks under ``news::<key>::<item>``; the
    middle segment is the per-source key (see ``ingest_world.source_key``).
    Returns ``""`` for ids that don't follow the scheme.
    """
    parts = (source_id or "").split("::")
    if len(parts) >= 3 and parts[0] == "news":
        return parts[1]
    return ""


def _group_world_items(chunks: list[dict]) -> dict[str, list[dict]]:
    """Reassemble world chunks into per-source items, keyed by source key.

    Chunks sharing a ``source_id`` are one item (a video transcript or an
    article) that was sliding-window split at ingest; we concatenate their text
    back together. ``fetch_around`` returns no chunk-order index, so multi-chunk
    items may join slightly out of order — acceptable for summarisation, where
    the LLM works from the gist and the windows overlap anyway.

    Returns ``{source_key: [{"title", "text", "date"}, …]}`` preserving the
    newest-first order ``fetch_around`` yields.
    """
    by_item: dict[str, dict] = {}
    order: list[str] = []
    for chunk in chunks:
        source_id = chunk.get("source_id") or ""
        if source_id not in by_item:
            by_item[source_id] = {
                "key": _parse_world_source_key(source_id),
                "title": chunk.get("title") or "",
                "date": chunk.get("date") or "",
                "texts": [],
            }
            order.append(source_id)
        text = (chunk.get("text") or "").strip()
        if text:
            by_item[source_id]["texts"].append(text)

    grouped: dict[str, list[dict]] = {}
    for source_id in order:
        item = by_item[source_id]
        joined = " ".join(item["texts"]).strip()
        if not joined:
            continue
        grouped.setdefault(item["key"], []).append(
            {"title": item["title"], "text": joined, "date": item["date"]}
        )
    return grouped


async def _fetch_world_today(day: date, limit: int = 400) -> list[dict]:
    """Today's ``world``-corpus chunks (news / RSS / video) via ``fetch_around``.

    ``forward_days=0`` caps the look-ahead so a back-filled / regenerated past day
    (``ESTORMI_BRIEFING_DATE``) can't pull genuine D+1 world news into day D's
    briefing — the same guard the personal look-back fetches use in ``day_context``.
    ``window_days`` stays the look-BACK so the timezone-skew tolerance is kept on
    the lag side. Without this cap the leaked dates also get whitelisted by the
    date-lint and propagate into the distill training corpus.
    """
    return await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": _WORLD_WINDOW_DAYS,
            "forward_days": 0,
            "corpus": "world",
            "limit": limit,
        },
        timeout=20.0,
    )


async def _fetch_world_followup(query: str, limit: int = _WORLD_FOLLOWUP_LIMIT) -> list[dict]:
    """Past world-corpus chunks related to today's topics — the "suivi" pass.

    A semantic search across the *whole* world corpus (no time window) so the
    day-vision can connect a developing story to how it was covered before.
    Best-effort: returns ``[]`` on empty query or any search failure.
    """
    query = (query or "").strip()
    if not query:
        return []
    return await _search_mcp_memory({"query": query[:500], "corpus": "world", "limit": limit})
