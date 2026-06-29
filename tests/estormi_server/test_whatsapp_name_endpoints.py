"""resolve-names / backfill-titles: WhatsApp name resolution endpoints.

These close the ingest-time race where a brand-new chat's first messages are
chunked under a raw JID because the chat-list enrichment hasn't created its row
yet — even though the contact is in the macOS address book. ``resolve-names``
resolves ids directly against Contacts before ingestion; ``backfill-titles``
heals chunks already stored with a raw-JID title once a name is available.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.regression]


async def test_resolve_names_resolves_phone_dm_via_contacts(wired_tools_db):
    from estormi_server.api import whatsapp_settings as ws

    db = wired_tools_db
    body = ws._ResolveNamesBody(chat_ids=["33687654321@s.whatsapp.net"])
    with patch(
        "estormi_server.integrations.macos_contacts.phone_name_index",
        return_value={"687654321": "Claire Dubois"},
    ):
        out = await ws.whatsapp_resolve_names(MagicMock(), body)

    assert out == {"33687654321@s.whatsapp.net": "Claire Dubois"}
    # Persisted so the chat list and briefing read the same name later.
    cur = await db.execute(
        "SELECT chat_name FROM whatsapp_chats WHERE chat_id = ?",
        ("33687654321@s.whatsapp.net",),
    )
    assert (await cur.fetchone())[0] == "Claire Dubois"


async def test_resolve_names_skips_unknown_numbers(wired_tools_db):
    from estormi_server.api import whatsapp_settings as ws

    body = ws._ResolveNamesBody(chat_ids=["33600000000@s.whatsapp.net"])
    with patch("estormi_server.integrations.macos_contacts.phone_name_index", return_value={}):
        out = await ws.whatsapp_resolve_names(MagicMock(), body)
    assert out == {}


async def test_backfill_titles_retitles_opaque_chunks_only(wired_tools_db):
    from estormi_server.api import whatsapp_settings as ws

    db = wired_tools_db
    # Orphan: title is still the raw JID; name is resolvable via Contacts.
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, source_id, title, chat_id_raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "c1",
            "h1",
            "whatsapp",
            "33687654321@s.whatsapp.net:t",
            "WhatsApp — 33687654321@s.whatsapp.net",
            "33687654321@s.whatsapp.net",
        ),
    )
    # Already named: must be left untouched even though its number resolves.
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, source_id, title, chat_id_raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("c2", "h2", "whatsapp", "x:t", "WhatsApp — Alice", "33611223344@s.whatsapp.net"),
    )
    await db.commit()

    with patch(
        "estormi_server.integrations.macos_contacts.phone_name_index",
        return_value={"687654321": "Claire Dubois"},
    ):
        out = await ws.whatsapp_backfill_titles(MagicMock())

    assert out == {"updated": 1}
    cur = await db.execute("SELECT title FROM chunks WHERE id = 'c1'")
    assert (await cur.fetchone())[0] == "WhatsApp — Claire Dubois"
    cur = await db.execute("SELECT title FROM chunks WHERE id = 'c2'")
    assert (await cur.fetchone())[0] == "WhatsApp — Alice"


async def test_backfill_titles_no_op_when_nothing_opaque(wired_tools_db):
    from estormi_server.api import whatsapp_settings as ws

    db = wired_tools_db
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, source_id, title, chat_id_raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("c3", "h3", "whatsapp", "x:t", "WhatsApp — Bob", "33600000000@s.whatsapp.net"),
    )
    await db.commit()
    with patch("estormi_server.integrations.macos_contacts.phone_name_index", return_value={}):
        out = await ws.whatsapp_backfill_titles(MagicMock())
    assert out == {"updated": 0}
