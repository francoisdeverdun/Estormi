"""WhatsApp sender / @mention name resolution at ingestion.

Covers the helpers that turn raw WhatsApp handles into human names so the
briefing attributes messages correctly and never leaks a mention's digit run
into a PII false-positive.
"""

from __future__ import annotations

import pytest

from estormi_ingestion.whatsapp import ingest_conversations as ic

pytestmark = pytest.mark.unit


@pytest.fixture
def name_cache(monkeypatch):
    cache = {
        "33612345678@s.whatsapp.net": "Alice Martin",
        "100000000000001@lid": "Claire Dubois",
    }
    monkeypatch.setattr(ic, "_chat_name_cache", cache)
    return cache


def test_name_for_handle_resolves_phone_and_lid(name_cache):
    assert ic._name_for_handle("33612345678") == "Alice Martin"
    assert ic._name_for_handle("100000000000001@lid") == "Claire Dubois"
    assert ic._name_for_handle("999111222@lid") == ""
    assert ic._name_for_handle("") == ""


def test_resolve_mentions_known_and_unknown(name_cache):
    assert ic._resolve_mentions("@33612345678 tu viens ?") == "@Alice Martin tu viens ?"
    # An unknown 15-digit @lid mention collapses to @… so the bare digit run
    # never survives to be mistaken for an NIR / card number downstream.
    assert ic._resolve_mentions("@100000000000002 ?") == "@… ?"


def test_display_name_resolves_known_group_lid_sender(name_cache):
    # A group member staged as a raw @lid handle we know from a DM gets named.
    assert ic._display_name("100000000000001@lid", "120363@g.us") == "Claire Dubois"
    # An unknown group @lid stays as-is — WhatsApp transmits no name for it.
    assert ic._display_name("100000000000002@lid", "120363@g.us") == "100000000000002@lid"


def test_display_name_resolves_opaque_dm_sender(name_cache):
    # A bare-number / [unknown] sender in a 1:1 DM resolves to the chat partner.
    name_cache["33698765432@s.whatsapp.net"] = "Bob Durand"
    assert ic._display_name("unknown", "33698765432@s.whatsapp.net") == "Bob Durand"


def test_format_sub_window_resolves_mention_without_pii_redaction(name_cache):
    sub = [
        {
            "name": "Me",
            "chat_id": "120363@g.us",
            "text": "@100000000000002 du coup, tu viens finalement ou pas ?",
        }
    ]
    out = ic._format_sub_window(sub)
    assert out.startswith("[Me]:")
    assert "REDACTED:SOCIAL_SECURITY" not in out
    assert "@…" in out
