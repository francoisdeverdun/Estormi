"""Regression tests for ``ingest_chunk`` dedupe + stale-replace semantics.

Two bugs flagged in the v1.8 review:

1. **Partial-overlap stale-delete.** The replacement of old chunks for the
   same ``source_id`` only fired when *every* existing row was stale. In the
   partial-overlap case the new chunk was inserted alongside the obsolete
   ones, leaving duplicated content under the same source_id.

2. **UNIQUE(content_hash) race.** Concurrent ingestion of the same chunk
   could pass the pre-check ``SELECT`` but fail the ``INSERT`` on the unique
   index, surfacing a 500 instead of a clean "duplicate".

The fix keeps the SELECT as a fast-path and wraps the INSERT in
``aiosqlite.IntegrityError`` handling so the loser returns
``status=skipped reason=duplicate``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


async def test_partial_overlap_replaces_only_stale_rows(wired_tools_db, mock_qdrant):
    """Mixed-family old rows: only the stale-family ones get dropped.

    We construct the partial-overlap state by inserting two rows under one
    source_id whose hash families differ (``oldfamily-…`` vs ``newfamily-…``).
    Re-ingesting a chunk whose family matches ``newfamily`` must drop only
    the ``oldfamily-…`` row — the previous implementation skipped the delete
    entirely (``len(stale) != len(old_rows)``) and left both rows behind,
    causing duplicated content under one source_id.
    """
    from estormi_server.storage import writers

    db = wired_tools_db
    # Pre-seed two rows by hand so we control the hash families exactly.
    # (Going through ingest_chunk a second time would replace, not add.)
    await db.execute(
        """
        INSERT INTO chunks (
          id, content_hash, source, source_id, title, date, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("id-old", "oldfamily-0", "docs", "doc1", "stale", ""),
    )
    await db.execute(
        """
        INSERT INTO chunks (
          id, content_hash, source, source_id, title, date, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("id-keep", "newfamily-0", "docs", "doc1", "current", ""),
    )
    await db.commit()

    # Re-ingest under the "newfamily" base. The new chunk has a distinct
    # hash (newfamily-1) but the same family as id-keep.
    result = await writers.ingest_chunk(
        text="a fresh chunk in the current document",
        source="docs",
        content_hash="newfamily-1",
        source_id="doc1",
    )
    assert result["status"] == "ok"

    cur = await db.execute(
        "SELECT id, content_hash FROM chunks WHERE source_id = 'doc1' ORDER BY id"
    )
    rows_after = await cur.fetchall()
    await cur.close()
    hashes_after = {r["content_hash"] for r in rows_after}
    ids_after = {r["id"] for r in rows_after}

    # The current-family row stays, the new chunk is inserted, the stale
    # row is gone. Previously: id-old would still be present.
    assert "oldfamily-0" not in hashes_after, "stale-family row should be gone"
    assert "newfamily-0" in hashes_after
    assert "newfamily-1" in hashes_after
    assert "id-keep" in ids_after
    assert "id-old" not in ids_after
    assert len(rows_after) == 2


async def test_integrity_error_race_returns_clean_duplicate(wired_tools_db, mock_qdrant):
    """A concurrent winner triggers IntegrityError → return duplicate, not 500."""
    from estormi_server.storage import writers

    db = wired_tools_db
    # Pre-seed a row so the UNIQUE(content_hash) constraint fires the
    # second time we try to insert the same hash. The real db.execute is
    # forced to raise IntegrityError on the *second* INSERT only, after
    # the SELECT short-circuit has been bypassed by stubbing it.

    # First ingest — succeeds normally.
    first = await writers.ingest_chunk(
        text="hello world",
        source="notes",
        content_hash="race-1",
        source_id="note-A",
    )
    assert first["status"] == "ok"

    # Patch the dedupe SELECT to "miss" so the INSERT path actually runs.
    # The pre-existing row will collide on the UNIQUE index.
    original_execute = db.execute

    select_calls = {"count": 0}

    async def fake_execute(sql, *args, **kwargs):
        if (
            isinstance(sql, str)
            and sql.startswith("SELECT id FROM chunks WHERE content_hash")
            and select_calls["count"] == 0
        ):
            select_calls["count"] += 1
            return await original_execute("SELECT id FROM chunks WHERE 0")
        return await original_execute(sql, *args, **kwargs)

    with patch.object(db, "execute", side_effect=fake_execute):
        second = await writers.ingest_chunk(
            text="hello world (race winner already inserted)",
            source="notes",
            content_hash="race-1",  # collision
            source_id="note-A",
        )

    assert second["status"] == "skipped"
    assert second["reason"] == "duplicate"
    # The Qdrant rollback must have been attempted.
    assert mock_qdrant.delete.await_count >= 1
