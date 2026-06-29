"""Apple Mail chunks carry a ``chat_id_raw`` column so messages in one mail
THREAD can be grouped together during retrieval (``fetch_around``). For Apple
Mail the natural conversation unit is the mail THREAD, identified by RFC822
message-id threading headers (References / In-Reply-To / Message-ID).

``thread_root_key`` used to live inside a shell heredoc in
``estormi_ingestion/apple_mail/watch_and_ingest.sh`` and could not be imported, so
this test once replicated the function body verbatim and AST-compared it against
the shell source to catch drift. The body is now an importable module
(``estormi_ingestion.apple_mail.ingest``), so the replica + drift guard are gone:
the test imports and exercises the real function directly.
"""

from __future__ import annotations

import hashlib

import pytest

from estormi_ingestion.apple_mail.ingest import thread_root_key

pytestmark = pytest.mark.unit


def _key_of(message_id: str) -> str:
    """Expected stable key for a (bracket-free) message-id."""
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_references_uses_first_message_id():
    """References with multiple ids → the FIRST id roots the thread."""
    headers = (
        "From: alice@example.com\n"
        "References: <root@example.com> <reply1@example.com> <reply2@example.com>\n"
        "In-Reply-To: <reply2@example.com>\n"
        "Message-ID: <reply3@example.com>\n"
    )
    assert thread_root_key(headers, "src-1") == _key_of("root@example.com")


@pytest.mark.unit
def test_in_reply_to_used_when_no_references():
    """No References header → fall back to the In-Reply-To id."""
    headers = (
        "From: bob@example.com\n"
        "In-Reply-To: <parent@example.com>\n"
        "Message-ID: <child@example.com>\n"
    )
    assert thread_root_key(headers, "src-2") == _key_of("parent@example.com")


@pytest.mark.unit
def test_message_id_used_when_no_references_or_in_reply_to():
    """Neither References nor In-Reply-To → use the message's own Message-ID."""
    headers = "From: carol@example.com\nMessage-ID: <self@example.com>\n"
    assert thread_root_key(headers, "src-3") == _key_of("self@example.com")


@pytest.mark.unit
def test_falls_back_to_source_id_when_no_headers():
    """No headers at all → fall back to the per-email source_id."""
    assert thread_root_key("", "src-4") == "src-4"
    assert thread_root_key(None, "src-5") == "src-5"


@pytest.mark.unit
def test_header_parsing_is_case_insensitive():
    """Header names are matched case-insensitively."""
    headers = "rEfErEnCeS: <root@example.com>\n"
    assert thread_root_key(headers, "src-6") == _key_of("root@example.com")


@pytest.mark.unit
def test_garbled_headers_fall_back_to_source_id():
    """Garbled headers with no message-id token never crash; fall back."""
    assert thread_root_key("not a header block at all", "src-7") == "src-7"
    assert thread_root_key("References:\nIn-Reply-To:\n", "src-8") == "src-8"
