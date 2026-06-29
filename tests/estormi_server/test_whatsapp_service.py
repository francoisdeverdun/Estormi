"""Service-layer tests for :mod:`estormi_server.services.whatsapp`.

The router (``api/whatsapp_settings``) is a thin shell; the SQL + business
logic lives here and was previously exercised only indirectly. These tests
drive the service functions directly against the in-memory ``db`` fixture with
every external boundary (sidecar HTTP, macOS Contacts, the local LLM,
chunk/vector deletes) mocked — no real WhatsApp data, no network, no model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from estormi_server.services import whatsapp as wa
from memory_core.labels import is_opaque_label

pytestmark = pytest.mark.integration


# ── Fake sidecar HTTP client ────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal async-context-manager stand-in for ``httpx.AsyncClient``."""

    def __init__(self, *, resp: _FakeResp | None = None, raise_exc: Exception | None = None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        if self._raise is not None:
            raise self._raise
        return self._resp


def _patch_sidecar(client: _FakeClient):
    """Patch the sidecar HTTP client + header builder used by the service."""
    return (
        patch("estormi_server.services.whatsapp.httpx.AsyncClient", return_value=client),
        patch("estormi_server.services.whatsapp.sidecar_headers", return_value={}),
    )


# ── Pure helpers ────────────────────────────────────────────────────────────
class TestPureHelpers:
    def test_format_phone_jid_numeric_userpart(self):
        assert wa._format_phone_jid("33612345678@s.whatsapp.net") == "+33612345678"

    def test_format_phone_jid_non_numeric_falls_back_to_raw(self):
        # @lid / @g.us userparts aren't phone numbers — return the JID verbatim.
        assert wa._format_phone_jid("abc@lid") == "abc@lid"
        assert wa._format_phone_jid("120363@g.us") == "+120363"  # digits → formatted

    def test_is_masked_pushname_detects_bullet_mask(self):
        assert wa._is_masked_pushname("+33 ∙∙ ∙∙ 11") is True
        assert wa._is_masked_pushname("Alice Martin") is False

    def test_is_opaque_label(self):
        # Shared helper (memory_core.labels) — exercised here against the
        # WhatsApp JID shapes this service feeds it.
        assert is_opaque_label("") is True
        assert is_opaque_label("   ") is True
        assert is_opaque_label("33612345678@s.whatsapp.net") is True
        assert is_opaque_label("120363@g.us") is True
        assert is_opaque_label("+33 6 12 34 56 78") is True
        assert is_opaque_label("Alice Martin") is False


class TestDisplayName:
    def test_prefers_macos_contacts_for_dm(self):
        contacts = {"33612345678": "Alice Martin"}
        with patch(
            "estormi_server.integrations.macos_contacts.name_for_phone", return_value="Alice Martin"
        ):
            label = wa._wa_display_name("33612345678@s.whatsapp.net", "stored push name", contacts)
        assert label == "Alice Martin"

    def test_uses_stored_name_when_no_contact(self):
        label = wa._wa_display_name("120363@g.us", "Family Group")
        assert label == "Family Group"

    def test_skips_masked_stored_name_then_status_then_phone(self):
        # Masked stored name is ignored; a non-masked status wins next.
        label = wa._wa_display_name(
            "33612345678@s.whatsapp.net",
            "+33 ∙∙ ∙∙ 11",
            None,
            {"33612345678@s.whatsapp.net": "Available"},
        )
        assert label == "Available"

    def test_falls_back_to_formatted_phone_for_dm(self):
        label = wa._wa_display_name("33612345678@s.whatsapp.net", "")
        assert label == "+33612345678"

    def test_falls_back_to_raw_jid_for_group(self):
        label = wa._wa_display_name("120363@g.us", "")
        assert label == "120363@g.us"


# ── enrich_chat_names_from_sidecar ──────────────────────────────────────────
class TestEnrichFromSidecar:
    async def test_sidecar_unreachable_returns_empty(self, db):
        client = _FakeClient(raise_exc=RuntimeError("connection refused"))
        p_client, p_headers = _patch_sidecar(client)
        with p_client, p_headers:
            out = await wa.enrich_chat_names_from_sidecar(db)
        assert out == {}

    async def test_non_200_returns_empty(self, db):
        client = _FakeClient(resp=_FakeResp(503, None))
        p_client, p_headers = _patch_sidecar(client)
        with p_client, p_headers:
            out = await wa.enrich_chat_names_from_sidecar(db)
        assert out == {}

    async def test_upserts_named_chats_and_returns_statuses(self, db):
        payload = [
            {"id": "120363@g.us", "name": "Family Group"},
            {"id": "33612345678@s.whatsapp.net", "pushname": "Alice", "status": "Available"},
            {"id": "999@g.us", "name": "+33 ∙∙ ∙∙ 11"},  # masked → skipped
            {"id": "", "name": "noid"},  # no id → skipped
        ]
        client = _FakeClient(resp=_FakeResp(200, payload))
        p_client, p_headers = _patch_sidecar(client)
        with p_client, p_headers:
            statuses = await wa.enrich_chat_names_from_sidecar(db)

        assert statuses == {"33612345678@s.whatsapp.net": "Available"}
        rows = await db.execute_fetchall("SELECT chat_id, chat_name FROM whatsapp_chats")
        stored = {cid: name for cid, name in rows}
        assert stored["120363@g.us"] == "Family Group"
        assert stored["33612345678@s.whatsapp.net"] == "Alice"
        assert "999@g.us" not in stored  # masked name never persisted

    async def test_clears_previously_masked_db_names(self, db):
        await db.execute(
            "INSERT INTO whatsapp_chats (chat_id, chat_name) VALUES (?, ?)",
            ("120363@g.us", "+33 ∙∙ ∙∙ 11"),
        )
        await db.commit()
        client = _FakeClient(resp=_FakeResp(200, []))  # no rows
        p_client, p_headers = _patch_sidecar(client)
        with p_client, p_headers:
            await wa.enrich_chat_names_from_sidecar(db)
        row = await db.execute_fetchall(
            "SELECT chat_name FROM whatsapp_chats WHERE chat_id = '120363@g.us'"
        )
        assert row[0][0] == ""


# ── list_chats ──────────────────────────────────────────────────────────────
class TestListChats:
    async def test_resolves_and_sorts_labels(self, db):
        await db.executemany(
            "INSERT INTO whatsapp_chats (chat_id, chat_name, group_type, chat_kind) "
            "VALUES (?, ?, ?, ?)",
            [
                ("120363@g.us", "Zeta Group", "friends", "group"),
                ("33612345678@s.whatsapp.net", "Alice", "family", "dm"),
            ],
        )
        await db.commit()
        with (
            patch.object(wa, "enrich_chat_names_from_sidecar", new=AsyncMock(return_value={})),
            patch("estormi_server.integrations.macos_contacts.phone_name_index", return_value={}),
            patch("estormi_server.integrations.macos_contacts.name_for_phone", return_value=None),
        ):
            chats = await wa.list_chats(db)

        names = [c["chat_name"] for c in chats]
        assert names == sorted(names, key=str.casefold)
        assert {c["chat_id"] for c in chats} == {
            "120363@g.us",
            "33612345678@s.whatsapp.net",
        }

    async def test_persists_resolved_contact_name(self, db):
        await db.execute(
            "INSERT INTO whatsapp_chats (chat_id, chat_name) VALUES (?, ?)",
            ("33612345678@s.whatsapp.net", "old"),
        )
        await db.commit()
        with (
            patch.object(wa, "enrich_chat_names_from_sidecar", new=AsyncMock(return_value={})),
            patch(
                "estormi_server.integrations.macos_contacts.phone_name_index",
                return_value={"33612345678": "Alice Martin"},
            ),
            patch(
                "estormi_server.integrations.macos_contacts.name_for_phone",
                return_value="Alice Martin",
            ),
        ):
            await wa.list_chats(db)
        row = await db.execute_fetchall(
            "SELECT chat_name FROM whatsapp_chats WHERE chat_id = '33612345678@s.whatsapp.net'"
        )
        assert row[0][0] == "Alice Martin"


# ── resolve / backfill ──────────────────────────────────────────────────────
class TestResolveAndBackfill:
    async def test_resolve_and_persist_names(self, db):
        with (
            patch(
                "estormi_server.integrations.macos_contacts.phone_name_index",
                return_value={"33612345678": "Alice Martin"},
            ),
            patch(
                "estormi_server.integrations.macos_contacts.name_for_phone",
                return_value="Alice Martin",
            ),
        ):
            out = await wa.resolve_and_persist_names(db, ["33612345678@s.whatsapp.net"])
        assert out == {"33612345678@s.whatsapp.net": "Alice Martin"}
        row = await db.execute_fetchall(
            "SELECT chat_name FROM whatsapp_chats WHERE chat_id = '33612345678@s.whatsapp.net'"
        )
        assert row[0][0] == "Alice Martin"

    async def test_resolve_empty_input_is_noop(self, db):
        out = await wa.resolve_and_persist_names(db, [])
        assert out == {}

    async def test_backfill_titles_rewrites_opaque_titles(self, db):
        await db.execute(
            "INSERT INTO whatsapp_chats (chat_id, chat_name) VALUES (?, ?)",
            ("33612345678@s.whatsapp.net", "Alice Martin"),
        )
        await db.execute(
            "INSERT INTO chunks (id, content_hash, source, chat_id_raw, title) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "c1",
                "h1",
                "whatsapp",
                "33612345678@s.whatsapp.net",
                "WhatsApp — 33612345678@s.whatsapp.net",
            ),
        )
        await db.commit()
        with (
            patch("estormi_server.integrations.macos_contacts.phone_name_index", return_value={}),
            patch("estormi_server.integrations.macos_contacts.name_for_phone", return_value=None),
        ):
            updated = await wa.backfill_titles(db)
        assert updated == 1
        row = await db.execute_fetchall("SELECT title FROM chunks WHERE id = 'c1'")
        assert row[0][0] == "WhatsApp — Alice Martin"

    async def test_backfill_titles_noop_when_nothing_opaque(self, db):
        await db.execute(
            "INSERT INTO chunks (id, content_hash, source, chat_id_raw, title) "
            "VALUES (?, ?, ?, ?, ?)",
            ("c2", "h2", "whatsapp", "33612345678@s.whatsapp.net", "WhatsApp — Alice Martin"),
        )
        await db.commit()
        with (
            patch("estormi_server.integrations.macos_contacts.phone_name_index", return_value={}),
            patch("estormi_server.integrations.macos_contacts.name_for_phone", return_value=None),
        ):
            assert await wa.backfill_titles(db) == 0


# ── set_chat_group_type / wipe ──────────────────────────────────────────────
class TestGroupTypeAndWipe:
    async def test_set_chat_group_type_persists_and_retags(self, db):
        await db.execute(
            "INSERT INTO whatsapp_chats (chat_id, chat_name, group_type) VALUES (?, ?, ?)",
            ("120363@g.us", "Family", "unknown"),
        )
        await db.commit()
        with patch(
            "estormi_server.storage.chunk_admin.retag_chunks",
            new=AsyncMock(return_value={"retagged": 3}),
        ):
            n = await wa.set_chat_group_type(db, "120363@g.us", "family")
        assert n == 3
        row = await db.execute_fetchall(
            "SELECT group_type FROM whatsapp_chats WHERE chat_id = '120363@g.us'"
        )
        assert row[0][0] == "family"

    async def test_wipe_whatsapp_log(self, db):
        await db.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at) "
            "VALUES ('whatsapp_log', '2026-01-01')"
        )
        await db.execute(
            "INSERT INTO whatsapp_messages (msg_id, chat_id, ts_iso, sender_name, text) "
            "VALUES ('m1', '120363@g.us', '2026-01-01', 's', 't')"
        )
        await db.commit()
        with (
            patch(
                "estormi_server.storage.writers.delete_by_source",
                new=AsyncMock(return_value={"deleted": 7}),
            ),
            patch("estormi_server.services.whatsapp.WA_STAGING_PATH") as staging,
        ):
            staging.exists.return_value = False
            deleted = await wa.wipe_whatsapp_log(db)
        assert deleted == 7
        rows = await db.execute_fetchall("SELECT * FROM whatsapp_messages")
        assert rows == []


# ── Auto-tagger ─────────────────────────────────────────────────────────────
class TestAutotag:
    async def test_classify_chat_matches_known_label(self):
        with patch(
            "memory_core.llm_local.chat_completion",
            new=AsyncMock(return_value=" Work.\n"),
        ):
            assert await wa._classify_chat("Acme team", "let's ship it") == "work"

    async def test_classify_chat_accepts_us_organization_alias(self):
        with patch(
            "memory_core.llm_local.chat_completion",
            new=AsyncMock(return_value="organization"),
        ):
            assert await wa._classify_chat("PTA", "meeting notes") == "organisation"

    async def test_classify_chat_unknown_output_returns_none(self):
        with patch(
            "memory_core.llm_local.chat_completion",
            new=AsyncMock(return_value="something else entirely"),
        ):
            assert await wa._classify_chat("x", "y") is None

    async def test_classify_chat_llm_error_returns_none(self):
        with patch(
            "memory_core.llm_local.chat_completion",
            new=AsyncMock(side_effect=RuntimeError("model down")),
        ):
            assert await wa._classify_chat("x", "y") is None

    async def test_begin_and_status_payload(self):
        wa.begin_autotag_run()
        payload = wa.autotag_status_payload()
        assert payload["running"] is True
        assert payload["tagged"] == 0
        # The payload is a copy, not the live state dict.
        payload["tagged"] = 999
        assert wa.autotag_status_payload()["tagged"] == 0

    async def test_run_autotag_tags_chats(self, wired_tools_db):
        db = wired_tools_db
        await db.execute(
            "INSERT INTO whatsapp_chats (chat_id, chat_name, group_type) VALUES (?, ?, ?)",
            ("120363@g.us", "Acme team", "unknown"),
        )
        await db.commit()
        with (
            patch.object(wa, "enrich_chat_names_from_sidecar", new=AsyncMock(return_value={})),
            patch.object(wa, "_sample_chat_text", new=AsyncMock(return_value="ship the release")),
            patch.object(wa, "_classify_chat", new=AsyncMock(return_value="work")),
            patch(
                "estormi_server.storage.chunk_admin.retag_chunks",
                new=AsyncMock(return_value={"retagged": 0}),
            ),
        ):
            await wa.run_autotag(only_unknown=True)

        row = await db.execute_fetchall(
            "SELECT group_type FROM whatsapp_chats WHERE chat_id = '120363@g.us'"
        )
        assert row[0][0] == "work"
        state = wa.autotag_status_payload()
        assert state["tagged"] == 1
        assert state["running"] is False

    async def test_run_autotag_skips_chats_without_text(self, wired_tools_db):
        db = wired_tools_db
        await db.execute(
            "INSERT INTO whatsapp_chats (chat_id, chat_name, group_type) VALUES (?, ?, ?)",
            ("120363@g.us", "Quiet", "unknown"),
        )
        await db.commit()
        with (
            patch.object(wa, "enrich_chat_names_from_sidecar", new=AsyncMock(return_value={})),
            patch.object(wa, "_sample_chat_text", new=AsyncMock(return_value="")),
        ):
            await wa.run_autotag(only_unknown=True)
        state = wa.autotag_status_payload()
        assert state["skipped_no_text"] == 1
        assert state["tagged"] == 0
