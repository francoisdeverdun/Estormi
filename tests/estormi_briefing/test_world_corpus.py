"""World-corpus read path — content-date recency floor (W2).

The daily "🌍 Le monde" block windows on a chunk's ``date_ts`` (ingest/publish
timestamp). A late re-ingested item can carry a recent ``date_ts`` while its own
CONTENT date is months old — it then leaks into today's news. These tests cover
the content-date recency floor that drops the clearly-stale ones while leaving
recent and undated chunks untouched (degrade-soft).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from estormi_briefing.io import world_corpus

pytestmark = pytest.mark.unit


def _chunk(chunk_date: str | None, *, item: str = "a") -> dict:
    return {
        "source_id": f"news::lemonde::{item}",
        "title": f"Article {item}",
        "date": chunk_date,
        "text": f"Corps de l'article {item}.",
    }


def test_content_recency_floor_drops_clearly_stale_item():
    day = date(2026, 7, 1)
    # A 3-month-old content date — well past the 10-day floor — is dropped.
    chunks = [_chunk("2026-03-31", item="stale"), _chunk("2026-06-30", item="fresh")]
    kept = world_corpus._apply_content_recency_floor(chunks, day)
    kept_items = {c["source_id"] for c in kept}
    assert "news::lemonde::stale" not in kept_items
    assert "news::lemonde::fresh" in kept_items


def test_content_recency_floor_keeps_recent_and_boundary_items():
    day = date(2026, 7, 1)
    # Exactly at the floor (10 d before) is kept; one day past is dropped.
    at_floor = _chunk("2026-06-21", item="at_floor")  # 10 days → keep
    just_past = _chunk("2026-06-20", item="just_past")  # 11 days → drop
    kept = world_corpus._apply_content_recency_floor([at_floor, just_past], day)
    kept_items = {c["source_id"] for c in kept}
    assert "news::lemonde::at_floor" in kept_items
    assert "news::lemonde::just_past" not in kept_items


def test_content_recency_floor_keeps_undated_and_future_chunks():
    day = date(2026, 7, 1)
    undated = _chunk(None, item="undated")
    unparseable = _chunk("pas une date", item="unparseable")
    future = _chunk("2026-07-02", item="future")  # negative age → keep
    chunks = [undated, unparseable, future]
    kept = world_corpus._apply_content_recency_floor(chunks, day)
    # Degrade-soft: nothing we can't prove stale is dropped.
    assert len(kept) == 3


def test_content_recency_floor_never_empties_when_all_stale():
    """Even an all-stale batch is only filtered — the floor is a per-chunk
    predicate, never a fail-open-to-empty catastrophe. (Documents the contract:
    if every chunk is genuinely months old, the block is legitimately empty.)"""
    day = date(2026, 7, 1)
    chunks = [_chunk("2026-01-01", item="x"), _chunk("2026-02-01", item="y")]
    kept = world_corpus._apply_content_recency_floor(chunks, day)
    assert kept == []


async def test_fetch_world_today_applies_recency_floor():
    """The floor is wired into the fetch: a stale chunk from the DB is filtered
    before the caller ever groups it."""
    day = date(2026, 7, 1)
    raw = [_chunk("2026-03-31", item="stale"), _chunk("2026-06-30", item="fresh")]
    with patch.object(world_corpus, "_fetch_around_mcp", new_callable=AsyncMock, return_value=raw):
        out = await world_corpus._fetch_world_today(day)
    dates = {c["date"] for c in out}
    assert dates == {"2026-06-30"}
