"""run() orchestration — end-to-end pipeline behaviour.

The briefing engine reads world content (news / RSS / video) from the DB
(``corpus=world``, ingested by the ``knowledge`` DAG stage) instead of
fetching transcripts itself. These tests mock ``_fetch_world_today`` — the
single seam where the engine reads that corpus — rather than the old
yt-dlp / RSS fetch layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from estormi_briefing.run_briefing import run

pytestmark = pytest.mark.integration


def _world_chunk(key: str, item: str, *, title: str, text: str, date: str) -> dict:
    """A world-corpus chunk dict as ``_fetch_world_today`` would return it.

    ``source_id`` follows the ``news::<source_key>::<item>`` scheme that
    ``ingest_world`` writes and ``_group_world_items`` parses (the ``news::``
    prefix is an internal key, distinct from the ``knowledge`` source name).
    """
    return {
        "id": f"{key}-{item}",
        "source": "knowledge",
        "source_id": f"news::{key}::{item}",
        "title": title,
        "date": date,
        "date_ts": f"{date}T08:00:00+00:00",
        "end_date_ts": None,
        "group_type": "",
        "corpus": "world",
        "text": text,
    }


# ── run_knowledge: full pipeline (integration-level mock) ────────────────────


async def test_run_skips_briefing_when_no_world_or_actions(tmp_path, db_path, yaml_path):
    """Pipeline completes ok with '0 new items' when the world corpus is empty."""
    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        summary = await run(config_path=yaml_path)

    assert "0 new items" in summary
    mock_vault.assert_not_called()


async def test_run_ingests_briefing_when_world_item(tmp_path, db_path, yaml_path):
    """A world item for a configured source is summarised and the briefing ingested."""
    chunks = [
        _world_chunk("ch", "yt-vid1", title="T", text="Some transcript content", date="2026-05-02")
    ]
    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            return_value="- Fact bullet. (Ch, 2026-05-02)",
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        summary = await run(config_path=yaml_path)

    assert "1 new items" in summary
    mock_vault.assert_called_once()
    assert "Briefing" in mock_vault.call_args[0][0]["htmlBody"]


async def test_run_skips_world_item_with_empty_text(tmp_path, db_path, yaml_path):
    """A world item whose text is empty produces no LLM call and no briefing."""
    chunks = [_world_chunk("ch", "yt-vid1", title="T", text="   ", date="2026-05-02")]
    mock_llm = AsyncMock()
    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch("estormi_briefing.llm.runtime._llm_call", mock_llm),
        patch("estormi_briefing.run_briefing._vault_push_briefing"),
    ):
        summary = await run(config_path=yaml_path)

    mock_llm.assert_not_called()
    assert "0 new items" in summary


async def test_run_reassembles_multichunk_world_item(tmp_path, db_path, yaml_path):
    """Chunks sharing a source_id are concatenated into one item before
    summarisation — both fragments reach the same per-item LLM prompt."""
    chunks = [
        _world_chunk("ch", "yt-vid1", title="T", text="part one", date="2026-05-02"),
        _world_chunk("ch", "yt-vid1", title="T", text="part two", date="2026-05-02"),
    ]
    prompts: list[str] = []

    async def fake_llm(prompt, provider, model, **kwargs):
        prompts.append(prompt)
        return "- Merged fact."

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch("estormi_briefing.llm.runtime._llm_call", side_effect=fake_llm),
        patch("estormi_briefing.run_briefing._vault_push_briefing"),
    ):
        summary = await run(config_path=yaml_path)

    # Both fragments were concatenated into one item, so a single per-item
    # summarisation prompt carries both — later stages (theming) see only the
    # summarised bullet, hence the per-prompt search rather than the last call.
    assert any("part one" in p and "part two" in p for p in prompts)
    assert "1 new items" in summary


async def test_run_renders_summarised_content_into_briefing_html(tmp_path, db_path, yaml_path):
    """End-to-end: an ingested world chunk flows through grouping → per-item
    summary → render, and the summarised bullet text lands in the briefing
    HTML pushed to the vault. Only the LLM and the world-corpus read are
    mocked; grouping, theming, and rendering all run for real."""
    chunks = [
        _world_chunk(
            "ch",
            "yt-vid1",
            title="Quantum leap",
            text="A long transcript about a quantum computing breakthrough.",
            date="2026-05-02",
        )
    ]
    bullet = "- Quantum chip hits a new error-correction milestone. (Ch, 2026-05-02)"

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            return_value=bullet,
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        summary = await run(config_path=yaml_path)

    assert "1 new items" in summary
    mock_vault.assert_called_once()
    html = mock_vault.call_args[0][0]["htmlBody"]
    # The summarised content actually reached the rendered artifact — proof of
    # the full grouping → summary → render path, not just boilerplate.
    assert "error-correction milestone" in html


async def test_run_marks_status_error_on_exception(tmp_path, db_path, yaml_path):
    """A fatal crash in the pipeline writes 'error' to the settings table."""
    import aiosqlite

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        # load_sources runs before the per-source summarisation, so a failure
        # here is genuinely fatal and must surface as 'error'.
        patch(
            "estormi_briefing.run_briefing.load_sources",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await run(config_path=yaml_path)

    conn = await aiosqlite.connect(db_path)
    cur = await conn.execute("SELECT value FROM settings WHERE key = 'knowledge_last_run_status'")
    row = await cur.fetchone()
    await conn.close()
    assert row and row[0] == "error"


def _two_source_yaml(tmp_path):
    """A two-source YAML so isolation tests have a surviving source to ship."""
    p = tmp_path / "two_sources.yaml"
    p.write_text(
        "sources:\n"
        "  - id: ch\n    label: Ch\n    type: youtube_channel\n"
        "    url: https://www.youtube.com/@Ch/videos\n    axis: tech\n"
        "    mode: news\n    subtitle_langs: [fr]\n"
        "  - id: ch2\n    label: Ch2\n    type: youtube_channel\n"
        "    url: https://www.youtube.com/@Ch2/videos\n    axis: tech\n"
        "    mode: news\n    subtitle_langs: [fr]\n"
    )
    return p


async def test_run_isolates_single_source_failure(tmp_path, db_path):
    """One source raising does NOT abort the briefing — its partial is dropped
    while a surviving source still ships (the concurrency contract)."""
    import aiosqlite

    chunks = [
        _world_chunk("ch", "yt-vid1", title="T", text="boom content", date="2026-05-02"),
        _world_chunk("ch2", "yt-vid2", title="T2", text="good content", date="2026-05-02"),
    ]

    async def fake_summarize(source, world_items, provider, model, today):
        if source["id"] == "ch":
            raise RuntimeError("source boom")
        return {
            "items": [
                {
                    "axis": "tech",
                    "mode": "news",
                    "source_label": "Ch2",
                    "pre_prompt": "",
                    "bullets": ["- A surviving bullet."],
                }
            ],
            "total": 1,
            "rss_articles": 0,
            "youtube_videos": 1,
        }

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch("estormi_briefing.run_briefing._summarize_world_source", new=fake_summarize),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            return_value="<p>Theme.</p>",
        ),
        patch(
            "estormi_briefing.run_briefing._fetch_world_followup",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        # No raise: the guarded gather swallows the per-source error, and the
        # one good source still produces a briefing.
        summary = await run(config_path=_two_source_yaml(tmp_path))

    assert "2 sources, 1 new items" in summary
    mock_vault.assert_called_once()
    conn = await aiosqlite.connect(db_path)
    cur = await conn.execute("SELECT value FROM settings WHERE key = 'knowledge_last_run_status'")
    row = await cur.fetchone()
    await conn.close()
    assert row and row[0] == "ok"


async def test_run_marks_error_when_all_sources_collapse(tmp_path, db_path, yaml_path):
    """World material existed but every summary failed and there are no actions:
    a transient-outage collapse, flagged ``error`` — never shipped as ok."""
    import aiosqlite

    chunks = [_world_chunk("ch", "yt-vid1", title="T", text="content", date="2026-05-02")]
    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.run_briefing._summarize_world_source",
            side_effect=RuntimeError("source boom"),
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        summary = await run(config_path=yaml_path)

    assert "transient" in summary.lower()
    # The previous good briefing is preserved — no hollow overwrite.
    mock_vault.assert_not_called()
    conn = await aiosqlite.connect(db_path)
    cur = await conn.execute("SELECT value FROM settings WHERE key = 'knowledge_last_run_status'")
    row = await cur.fetchone()
    await conn.close()
    assert row and row[0] == "error"


async def test_run_collapse_guard_preserves_briefing_when_actions_present(
    tmp_path, db_path, yaml_path
):
    """Even with actions to render, a collapse (no summaries + no day-vision)
    does not overwrite the prior briefing with raw action lists."""
    from datetime import datetime as real_datetime

    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date, date_ts) "
        "VALUES ('rem1', 'h1', 'reminders', 'Task', "
        "'2026-05-02T07:30:00Z', '2026-05-02T07:30:00+00:00')"
    )
    await conn.commit()
    await conn.close()

    chunks = [_world_chunk("ch", "yt-vid1", title="T", text="content", date="2026-05-02")]
    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.run_briefing._summarize_world_source",
            side_effect=RuntimeError("source boom"),
        ),
        patch(
            "estormi_briefing.run_briefing._generate_day_vision",
            new_callable=AsyncMock,
            return_value=("", {}),
        ),
        patch("estormi_briefing.run_briefing.datetime") as mock_datetime,
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        import estormi_briefing.run_briefing as _rk

        mock_datetime.now.return_value = real_datetime(2026, 5, 2, 10, 0, tzinfo=_rk.LOCAL_TZ)
        mock_datetime.combine.side_effect = real_datetime.combine
        mock_datetime.fromisoformat.side_effect = real_datetime.fromisoformat
        summary = await run(config_path=yaml_path)

    assert "transient" in summary.lower()
    mock_vault.assert_not_called()
    conn = await aiosqlite.connect(db_path)
    cur = await conn.execute("SELECT value FROM settings WHERE key = 'knowledge_last_run_status'")
    row = await cur.fetchone()
    await conn.close()
    assert row and row[0] == "error"


async def test_run_ingests_actions_only_briefing(tmp_path, db_path, yaml_path):
    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        """
        INSERT INTO chunks (id, content_hash, source, title, date, date_ts)
        VALUES ('rem1', 'h1', 'reminders', 'Préparer le dossier', '2026-05-03T07:30:00Z', '2026-05-03T07:30:00+00:00')
        """
    )
    await conn.commit()
    await conn.close()

    from datetime import datetime as real_datetime

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=[],
        ),
        # Keep the day-vision enrichment pass off the network in tests.
        patch(
            "estormi_briefing.day.day_vision._compute_day_enrichments",
            new_callable=AsyncMock,
            return_value={"weather": "", "chained": []},
        ),
        patch("estormi_briefing.run_briefing.datetime") as mock_datetime,
        patch("estormi_briefing.run_briefing._vault_push_briefing") as mock_vault,
    ):
        import estormi_briefing.run_briefing as _rk

        mock_datetime.now.return_value = real_datetime(2026, 5, 3, 10, 0, tzinfo=_rk.LOCAL_TZ)
        mock_datetime.combine.side_effect = real_datetime.combine
        mock_datetime.fromisoformat.side_effect = real_datetime.fromisoformat
        summary = await run(config_path=yaml_path)

    assert "0 new items, 1 actions" in summary
    mock_vault.assert_called_once()
    # French-only edition: the default briefing language is French → the
    # fallback My-day heading is localised ("Ma journée").
    assert "Ma journée" in mock_vault.call_args[0][0]["htmlBody"]
    assert "Préparer le dossier" in mock_vault.call_args[0][0]["htmlBody"]


# ── run_knowledge: config_path override regression ────────────────────────────


async def test_run_explicit_config_path_not_overridden_by_data_dir(tmp_path, db_path, yaml_path):
    """Regression: an explicit config_path must not be silently replaced by
    whatever knowledge_sources.yaml happens to exist in DATA_DIR."""
    data_dir_yaml = tmp_path / "data_dir" / "knowledge_sources.yaml"
    data_dir_yaml.parent.mkdir(parents=True)
    data_dir_yaml.write_text(
        "sources:\n"
        "  - id: s1\n    label: S1\n    type: youtube_channel\n"
        "    url: https://youtube.com/@S1/videos\n    axis: tech\n"
        "    mode: news\n    subtitle_langs: [fr]\n"
        "  - id: s2\n    label: S2\n    type: youtube_channel\n"
        "    url: https://youtube.com/@S2/videos\n    axis: finance\n"
        "    mode: news\n    subtitle_langs: [fr]\n"
    )

    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('knowledge_llm_provider', 'local')"
    )
    await conn.commit()
    await conn.close()

    # World data only for the single source in yaml_path (key 'ch').
    chunks = [_world_chunk("ch", "yt-vid1", title="T", text="transcript", date="2026-05-03")]
    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch("estormi_briefing.run_briefing.DATA_DIR", str(data_dir_yaml.parent)),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            return_value="- Fact. (Ch, 2026-05-03)",
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing"),
    ):
        summary = await run(config_path=yaml_path)

    # Must see exactly 1 channel (from yaml_path), not 2 (from data_dir_yaml).
    assert "1 sources" in summary, f"Unexpected summary: {summary!r}"
    assert "1 new items" in summary, f"Unexpected summary: {summary!r}"


# ── run_knowledge: news/other split before consolidation ──────────────────────


async def test_run_news_items_not_consolidated_before_synthesis(tmp_path, db_path, yaml_path):
    """News items from different sources keep separate source_label entries
    when passed to _synthesize_news — consolidation must not merge them first."""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "sources:\n"
        "  - id: ch1\n    label: Le Monde\n    type: rss\n"
        "    urls: [https://example.com/rss]\n    axis: news\n"
        "    window_hours: 24\n"
        "  - id: ch2\n    label: Hugo Décrypte\n    type: youtube_channel\n"
        "    url: https://www.youtube.com/@hugodecrypteactus/videos\n    axis: news\n"
        "    mode: news\n    subtitle_langs: [fr]\n"
    )

    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('knowledge_llm_provider', 'local')"
    )
    await conn.commit()
    await conn.close()

    chunks = [
        _world_chunk("ch1", "art1", title="Art", text="Le Monde article", date="2026-05-15"),
        _world_chunk("ch2", "yt-v1", title="T", text="Hugo transcript", date="2026-05-15"),
    ]

    consolidate_calls: list[list[dict]] = []

    async def fake_consolidate(items, provider, model):
        consolidate_calls.append(list(items))
        return items

    synthesize_news_calls: list[list[dict]] = []

    async def fake_synthesize_news(
        items, date_str, provider, model, personal_context="", last_topics=""
    ):
        synthesize_news_calls.append(list(items))
        return "- Signal. [SOURCE: Le Monde]"

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", db_path),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            return_value='[{"kind":"news","text":"T","source":"src","date":"2026-05-15"}]',
        ),
        patch(
            "estormi_briefing.run_briefing._consolidate_items",
            side_effect=fake_consolidate,
        ),
        patch(
            "estormi_briefing.run_briefing._synthesize_news",
            side_effect=fake_synthesize_news,
        ),
        patch(
            "estormi_briefing.run_briefing._synthesize_themes",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing"),
    ):
        await run(config_path=yaml_path)

    # _consolidate_items must NOT have been called with any news (axis=news) items.
    for call_items in consolidate_calls:
        for item in call_items:
            assert item.get("axis") != "news", (
                f"News item unexpectedly passed to _consolidate_items: {item}"
            )

    # _synthesize_news must have been called with TWO separate items (one per source).
    assert synthesize_news_calls, "_synthesize_news was not called"
    news_call = synthesize_news_calls[0]
    source_labels = {item["source_label"] for item in news_call}
    assert "Le Monde" in source_labels
    assert "Hugo Décrypte" in source_labels


# ── fix: _extract_topics_from_items + always-persist logic ───────────────────


async def test_run_persists_topics_even_when_synthesis_empty(tmp_path):
    """knowledge_last_briefing_topics is written even if news_synthesis returns ''."""
    import aiosqlite
    import yaml

    db_path = tmp_path / "estormi.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.commit()

    cfg = {
        "sources": [
            {
                "id": "test_news",
                "label": "Test News",
                "type": "rss",
                "urls": ["https://example.com/feed"],
                "axis": "news",
                "mode": "news",
                "window_hours": 24,
            }
        ]
    }
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(yaml.dump(cfg), encoding="utf-8")

    chunks = [
        _world_chunk("test_news", "art1", title="T", text="Ukraine frappes", date="2026-05-16")
    ]
    news_bullet = (
        '[{"kind":"news","text":"Ukraine frappes","source":"Test News","date":"2026-05-16"}]'
    )

    async def fake_consolidate(items, provider, model):
        return items

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", str(db_path)),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            side_effect=[
                news_bullet,  # RSS summarisation → news bullet
                "",  # _synthesize_news → empty (simulates silent LLM failure)
            ],
        ),
        patch(
            "estormi_briefing.run_briefing._fetch_daily_actions",
            new_callable=AsyncMock,
            return_value={"calendar": [], "reminders": []},
        ),
        patch(
            "estormi_briefing.run_briefing._generate_day_vision",
            new_callable=AsyncMock,
            return_value=("", {}),
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing"),
        patch(
            "estormi_briefing.run_briefing._consolidate_items",
            side_effect=fake_consolidate,
        ),
    ):
        await run(config_path=yaml_path)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT value FROM settings WHERE key='knowledge_last_briefing_topics'"
        )
        row = await cur.fetchone()

    assert row is not None, (
        "knowledge_last_briefing_topics must be persisted even when synthesis is empty"
    )
    assert "Test News" in row[0], f"expected the source label in persisted topics, got {row[0]!r}"


async def test_run_persists_topics_from_all_items_when_no_news_items(tmp_path):
    """When news_items is empty, topics are persisted from all available items."""
    import aiosqlite
    import yaml

    db_path = tmp_path / "estormi.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.commit()

    cfg = {
        "sources": [
            {
                "id": "tech_src",
                "label": "Tech Source",
                "type": "rss",
                "urls": ["https://example.com/tech"],
                "axis": "tech",  # ← non-news axis
                "mode": "analysis",
                "window_hours": 24,
            }
        ]
    }
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(yaml.dump(cfg), encoding="utf-8")

    chunks = [_world_chunk("tech_src", "art1", title="T", text="GPT-5 annoncé", date="2026-05-16")]
    tech_bullet = (
        '[{"kind":"insight","text":"GPT-5 annoncé","source":"Tech Source","date":"2026-05-16"}]'
    )

    async def fake_consolidate(items, provider, model):
        return items

    with (
        patch("estormi_briefing.run_briefing.DB_PATH", str(db_path)),
        patch(
            "estormi_briefing.run_briefing._fetch_world_today",
            new_callable=AsyncMock,
            return_value=chunks,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            side_effect=[
                tech_bullet,  # RSS tech source → axis=tech (no news_items)
                "",  # _synthesize_themes → empty
            ],
        ),
        patch(
            "estormi_briefing.run_briefing._fetch_daily_actions",
            new_callable=AsyncMock,
            return_value={"calendar": [], "reminders": []},
        ),
        patch(
            "estormi_briefing.run_briefing._generate_day_vision",
            new_callable=AsyncMock,
            return_value=("", {}),
        ),
        patch("estormi_briefing.run_briefing._vault_push_briefing"),
        patch(
            "estormi_briefing.run_briefing._consolidate_items",
            side_effect=fake_consolidate,
        ),
    ):
        await run(config_path=yaml_path)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT value FROM settings WHERE key='knowledge_last_briefing_topics'"
        )
        row = await cur.fetchone()

    assert row is not None, "Topics must be persisted even when news_items is empty"
    assert "Tech Source" in row[0]


# ── critic→repair loop: merged deterministic + LLM verdicts ───────────────────


async def test_repair_loop_merges_lints_and_repairs(actions_db):
    """A draft with a provable structure violation must trigger a repair pass
    carrying the lint's directive, and the clean second draft must win."""
    from estormi_briefing.run_briefing import _run_critic_repair

    flawed = (
        "OBJECTIF : la journée.\n\n"
        "Un paragraphe correct qui décrit la journée avec assez de mots pour "
        "passer le seuil de densité du lint, en reliant les faits du jour entre "
        "eux comme il se doit, sans bullet ni rubrique, sur plusieurs phrases "
        "complètes qui disent ce que la journée signifie et ce qui est en jeu "
        "pour les engagements pris, le tout sans inventer quoi que ce soit.\n\n"
        "AROUND: rien aujourd'hui."
    )
    clean = flawed.replace("OBJECTIF :", "OBJECTIVE:")
    drafts = iter([(flawed, {"calendar": []}), (clean, {"calendar": []})])
    feedbacks: list[str] = []

    async def fake_vision(today, actions, provider, model, **kw):
        feedbacks.append(kw.get("critic_feedback") or "")
        return next(drafts)

    approved = '{"issues": [], "approved": true}'
    with (
        patch(
            "estormi_briefing.run_briefing._generate_day_vision",
            side_effect=fake_vision,
        ),
        patch(
            "estormi_briefing.llm.runtime._llm_call",
            new_callable=AsyncMock,
            return_value=approved,
        ),
        patch("estormi_briefing.llm.runtime._run_metrics", None),
    ):
        import estormi_briefing.llm.runtime as runtime

        runtime.refresh("fr", "")
        out, out_rows = await _run_critic_repair(
            actions_db, "2026-06-11", {"calendar": [], "reminders": []}, "", "local", "m"
        )

    # The flawed draft was lint-flagged (label_not_english) → a repair ran with
    # the directive in its feedback, and the clean draft was selected.
    assert len(feedbacks) == 2
    assert "label" in feedbacks[1] or "OBJECTIF" in feedbacks[1]
    assert out == clean
    assert out_rows == {"calendar": []}


async def test_repair_loop_passes_composer_flag(actions_db):
    """briefing_composer=single must reach _generate_day_vision as composer=False."""
    from estormi_briefing.run_briefing import _run_critic_repair

    await actions_db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('briefing_composer', 'single')"
    )
    await actions_db.commit()
    captured = {}

    async def fake_vision(today, actions, provider, model, **kw):
        captured.update(kw)
        return "", {}

    with (
        patch("estormi_briefing.run_briefing._generate_day_vision", side_effect=fake_vision),
        patch("estormi_briefing.llm.runtime._run_metrics", None),
    ):
        await _run_critic_repair(
            actions_db, "2026-06-11", {"calendar": [], "reminders": []}, "", "local", "m"
        )
    assert captured.get("use_composer") is False
