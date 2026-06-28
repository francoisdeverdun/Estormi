"""Split of WhatsApp group_type into semantic (group_type) + structural
(chat_kind) fields.

Covers the full path: ingestion writes the two axes separately, search exposes
an independent chat_kind filter, fetch_around returns it, the one-off SQLite
backfill demotes legacy structural group_type values, and the briefing's
behaviour-preserving `_wa_effective_type` shim reconstructs the pre-split value.
"""

from __future__ import annotations

import sqlite3

import pytest

from estormi_server.storage.search_api import fetch_around, search_memory
from estormi_server.storage.writers import ingest_chunk

# This module is a mix of two kinds, so markers are applied per item rather
# than blanket-marking the module: the ingest/search/fetch_around/migration
# tests cross the storage (DB + mocked Qdrant) boundary → ``integration``;
# the ``_wa_effective_type`` shim and the schema-contract class are pure
# functions over static data → ``unit``. (A single module-level marker would
# force one wrong label, and a class-level marker *unions* with — never
# overrides — the module one, which is exactly the double-marker removed here.)


# ── ingestion writes the two axes separately ────────────────────────────────


@pytest.mark.integration
async def test_ingest_writes_chat_kind_not_group_type(wired_tools_db, mock_qdrant):
    """A @g.us chat with no semantic tag → chat_kind='group', group_type unset."""
    await ingest_chunk(
        text="hello team",
        source="whatsapp",
        content_hash="ck-group-0",
        chat_id_raw="12345678901@g.us",
        date="2026-06-05T10:00:00+00:00",
    )
    # SQLite row
    cur = await wired_tools_db.execute(
        "SELECT group_type, chat_kind FROM chunks WHERE content_hash = ?", ("ck-group-0",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert row["chat_kind"] == "group"
    assert row["group_type"] is None  # semantic tag left unset, NOT 'group'
    # Qdrant payload
    payload = mock_qdrant.upsert.call_args.kwargs["points"][0].payload
    assert payload["chat_kind"] == "group"
    assert payload.get("group_type") is None


@pytest.mark.integration
async def test_ingest_semantic_and_structural_independent(wired_tools_db, mock_qdrant):
    await ingest_chunk(
        text="standup notes",
        source="whatsapp",
        content_hash="ck-both-0",
        chat_id_raw="12345678901@g.us",
        group_type="work",
    )
    payload = mock_qdrant.upsert.call_args.kwargs["points"][0].payload
    assert payload["group_type"] == "work"
    assert payload["chat_kind"] == "group"


# ── search exposes an independent chat_kind filter ──────────────────────────


@pytest.mark.integration
async def test_search_memory_builds_chat_kind_filter(mock_qdrant, mock_embedder):
    await search_memory("anything", chat_kind="dm")
    q_filter = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    keys = {c.key for c in q_filter.must}
    assert "chat_kind" in keys
    cond = next(c for c in q_filter.must if c.key == "chat_kind")
    assert cond.match.value == "dm"


@pytest.mark.integration
async def test_search_memory_no_chat_kind_no_filter(mock_qdrant, mock_embedder):
    await search_memory("anything")
    q_filter = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    keys = {c.key for c in q_filter.must} if q_filter else set()
    assert "chat_kind" not in keys


# ── fetch_around returns chat_kind (from SQLite) ────────────────────────────


@pytest.mark.integration
async def test_fetch_around_returns_chat_kind(wired_tools_db, mock_qdrant):
    await ingest_chunk(
        text="dinner plans",
        source="whatsapp",
        content_hash="ck-fa-0",
        chat_id_raw="33612345678@s.whatsapp.net",
        date="2026-06-05T12:00:00+00:00",
    )
    results = await fetch_around(date="2026-06-05", window_days=1)
    assert results, "expected the ingested chunk inside the window"
    assert results[0]["chat_kind"] == "dm"


# ── one-off SQLite backfill (sql.schema.MIGRATION_SQL) ──────────────────────


@pytest.mark.integration
def test_migration_backfills_and_demotes_structural_group_type(tmp_path):
    """A legacy chunk with the structural value in group_type is split correctly."""
    from estormi_server.sql.schema import INIT_SQL, MIGRATION_SQL  # noqa: PLC0415

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(INIT_SQL)  # fresh schema already carries chat_kind
    # Simulate a pre-split row: structural value sitting in the semantic column,
    # chat_kind still NULL.
    conn.execute(
        "INSERT INTO chunks (id, content_hash, source, chat_id_raw, group_type, chat_kind) "
        "VALUES ('1', 'h1', 'whatsapp', '999@g.us', 'group', NULL)"
    )
    conn.execute(
        "INSERT INTO whatsapp_chats (chat_id, chat_name, group_type, chat_kind) "
        "VALUES ('888@s.whatsapp.net', 'Bob', 'dm', NULL)"
    )
    conn.commit()
    conn.executescript(MIGRATION_SQL)  # one script — mirrors production startup
    conn.commit()

    row = conn.execute("SELECT group_type, chat_kind FROM chunks WHERE id='1'").fetchone()
    assert row == ("unknown", "group"), f"chunk not split: {row}"
    wa = conn.execute(
        "SELECT group_type, chat_kind FROM whatsapp_chats WHERE chat_id='888@s.whatsapp.net'"
    ).fetchone()
    assert wa == ("unknown", "dm"), f"whatsapp_chats not split: {wa}"
    conn.close()


# ── briefing behaviour-preservation shim ────────────────────────────────────


@pytest.mark.unit
def test_wa_effective_type_preserves_pre_split_classification():
    from estormi_briefing.day.day_context import (  # noqa: PLC0415
        _CONTEXT_WHATSAPP_GROUP_TYPES,
        _DAY_WHATSAPP_GROUP_TYPES,
        _wa_effective_type,
    )

    # A generic group, post-split, is {group_type:'unknown', chat_kind:'group'};
    # it must classify identically to a legacy {group_type:'group'} chunk:
    # context-only, never the actionable DAY set.
    post = {"group_type": "unknown", "chat_kind": "group"}
    assert _wa_effective_type(post) == "group"
    assert _wa_effective_type(post) in _CONTEXT_WHATSAPP_GROUP_TYPES
    assert _wa_effective_type(post) not in _DAY_WHATSAPP_GROUP_TYPES
    # A semantic tag always wins over the structural fallback.
    assert _wa_effective_type({"group_type": "work", "chat_kind": "group"}) == "work"
    # A genuinely-unknown chat (no chat_kind) stays 'unknown' (actionable DAY).
    assert _wa_effective_type({"group_type": "unknown", "chat_kind": None}) == "unknown"


# ── search_memory contract: semantic group_type vs structural chat_kind ───────


class TestSearchMemorySchemaContract:
    """U19 → chat_kind split: the search_memory tool schema must keep the
    *semantic* ``group_type`` and the *structural* ``chat_kind`` as separate,
    independently-filterable axes.

    Originally U19 widened the ``group_type`` enum to carry dm/group/broadcast;
    that conflated two orthogonal facts. The final design keeps ``group_type``
    semantic (work/family/…) and exposes a dedicated ``chat_kind`` filter
    (dm/group/broadcast) derived from the JID. These tests pin that contract.
    """

    pytestmark = pytest.mark.unit

    @staticmethod
    def _search_memory_props() -> dict:
        from estormi_server.api.mcp_rpc import TOOLS  # noqa: PLC0415

        tool = next(t for t in TOOLS if t["name"] == "search_memory")
        return tool["inputSchema"]["properties"]

    def test_group_type_enum_is_semantic_only(self):
        """The structural fallbacks must NOT pollute the semantic group_type enum."""
        enum = set(self._search_memory_props()["group_type"]["enum"])
        assert {"dm", "group", "broadcast"}.isdisjoint(enum), (
            f"structural values leaked into the semantic group_type enum: {enum}"
        )
        assert {
            "me",
            "partner",
            "work",
            "family",
            "couple",
            "friends",
            "organisation",
            "charity",
            "sport",
            "noise",
            "unknown",
        } <= enum

    def test_chat_kind_filter_exposes_structural_values(self):
        """A dedicated chat_kind filter must advertise exactly the JID-fallback kinds."""
        props = self._search_memory_props()
        assert "chat_kind" in props, "search_memory must expose a structural chat_kind filter"
        assert set(props["chat_kind"]["enum"]) == {"dm", "group", "broadcast"}

    def test_chat_kind_filter_covers_every_derivable_kind(self):
        """Every non-unknown value _chat_kind_from_jid yields is filterable."""
        from estormi_server.storage.writers import _chat_kind_from_jid  # noqa: PLC0415

        enum = set(self._search_memory_props()["chat_kind"]["enum"])
        derivable = {
            _chat_kind_from_jid("123@g.us"),  # group
            _chat_kind_from_jid("33612345678@s.whatsapp.net"),  # dm
            _chat_kind_from_jid("100000000000006@lid"),  # dm
            _chat_kind_from_jid("status@broadcast"),  # broadcast
        }
        assert derivable == {"group", "dm", "broadcast"}
        assert derivable <= enum
