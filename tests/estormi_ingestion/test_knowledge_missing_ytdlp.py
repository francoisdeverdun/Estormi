"""Regression: world ingestion survives a missing ``yt-dlp`` binary.

``_yt_dlp_bin()`` raises ``FileNotFoundError`` when yt-dlp isn't installed.
That exception must not crash the ``knowledge`` DAG stage — RSS-only sources
never need yt-dlp, so a YouTube source failing should be skipped, not fatal.

Fetching moved out of the briefing engine into ``estormi_ingestion/knowledge/
ingest_world.py`` (the DAG stage). These tests lock the contract there:

  * ``fetch_recent_videos`` itself still raises (yt-dlp is the only data path
    for YouTube), so the caller's catch is what matters;
  * ``_collect_world_items`` swallows the error and returns ``[]`` instead of
    letting it propagate and abort the stage.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def test_fetch_recent_videos_raises_filenotfound_when_ytdlp_missing(tmp_path):
    """``_yt_dlp_bin`` raises ``FileNotFoundError`` — the public function inherits it.

    The fix doesn't change this behaviour at the fetch layer; what it changes
    is that ``ingest_world.py`` catches it. Pin the raising-contract here so a
    later "swallow it at the fetch layer" change doesn't silently hide a real
    misconfiguration.
    """
    from estormi_ingestion.knowledge import knowledge_fetch  # noqa: PLC0415

    with (
        patch.object(knowledge_fetch.sys, "executable", str(tmp_path / "python")),
        patch.object(knowledge_fetch.shutil, "which", return_value=None),
    ):
        with pytest.raises(FileNotFoundError, match="yt-dlp"):
            knowledge_fetch.fetch_recent_videos(
                {"id": "yt:test", "url": "https://example.invalid/channel"},
                lookback_days=1,
            )


def test_collect_world_items_skips_youtube_when_ytdlp_missing():
    """``_collect_world_items`` swallows ``FileNotFoundError`` and returns ``[]``.

    A YouTube source whose fetch raises must yield no items rather than
    propagating — the DAG stage's per-source loop then moves on.
    """
    from estormi_ingestion.knowledge import ingest_world  # noqa: PLC0415

    source = {
        "id": "yt-should-fail",
        "type": "youtube_channel",
        "url": "https://example.invalid/channel",
        "axis": "news",
        "mode": "news",
        "label": "Test YT",
        "subtitle_langs": ["fr"],
    }

    def _raise(*args, **kwargs):
        raise FileNotFoundError("yt-dlp not found. Install it with: pip install yt-dlp")

    with patch.object(ingest_world, "fetch_recent_videos", side_effect=_raise):
        items = ingest_world._collect_world_items(source, lookback_days=1, today="2026-05-30")

    assert items == [], "a missing yt-dlp must yield no items, not crash the stage"


def test_ingest_world_module_has_filenotfound_catch_around_fetch():
    """The source still contains the catch — a refactor that drops it is a regression."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "packages"
        / "estormi_ingestion"
        / "knowledge"
        / "ingest_world.py"
    ).read_text()
    assert "fetch_recent_videos" in src
    assert "except FileNotFoundError" in src, (
        "ingest_world.py must catch FileNotFoundError so a missing yt-dlp "
        "doesn't crash the knowledge DAG stage"
    )
