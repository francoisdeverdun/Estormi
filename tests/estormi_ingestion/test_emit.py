"""Regression tests for shared.emit.post_chunks content-hash construction.

The server dedups GLOBALLY on ``content_hash`` (writers.ingest_chunk), so two
distinct sources/files with byte-identical text MUST produce different
content_hashes — otherwise the second is silently dropped as a duplicate. These
tests pin that the default base_hash folds ``source_id`` in (the bug a prior
"fix" missed by only touching an unreachable code path), and that re-emitting
the same ``(source_id, text)`` stays idempotent so genuine re-ingest still dedups.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from estormi_ingestion.shared import emit

pytestmark = pytest.mark.unit


def _posted_hashes(source_id: str, chunks: list[str]) -> list[str]:
    """Run post_chunks with the HTTP POST stubbed; return the content_hashes sent."""
    sent: list[str] = []

    def fake_post(url, payload, **_):
        sent.append(payload["content_hash"])
        return MagicMock(json=lambda: {"status": "ok"})

    with patch.object(emit.http_client, "post_chunk", side_effect=fake_post):
        emit.post_chunks("documents", source_id, chunks, mcp_url="http://x", title="t")
    return sent


def test_default_base_hash_differs_across_source_ids():
    """Identical text under two source_ids must not collide on content_hash."""
    a = _posted_hashes("/docs/a.md", ["byte-identical content"])
    b = _posted_hashes("/docs/b.md", ["byte-identical content"])
    assert a and b
    assert a[0] != b[0]


def test_default_base_hash_is_idempotent_per_source_id():
    """Same source_id + same text → same content_hash (so re-ingest dedups)."""
    a = _posted_hashes("/docs/a.md", ["byte-identical content"])
    b = _posted_hashes("/docs/a.md", ["byte-identical content"])
    assert a == b


def test_content_base_hash_folds_source_id():
    """The shared helper every producer routes through must never collide across
    source_ids on identical text (the global-dedup data-loss class), and must be
    deterministic for the same (source_id, text) so re-ingest stays idempotent."""
    a = emit.content_base_hash("/docs/a.md", "identical")
    b = emit.content_base_hash("/docs/b.md", "identical")
    assert a != b
    assert emit.content_base_hash("/docs/a.md", "identical") == a


def test_explicit_base_hash_is_honoured():
    """An explicit base_hash is used verbatim (callers that fold their own id)."""
    sent: list[str] = []

    def fake_post(url, payload, **_):
        sent.append(payload["content_hash"])
        return MagicMock(json=lambda: {"status": "ok"})

    with patch.object(emit.http_client, "post_chunk", side_effect=fake_post):
        emit.post_chunks("world", "sid", ["x"], mcp_url="http://x", title="t", base_hash="EXPLICIT")
    assert sent == ["EXPLICIT-0"]


def _posted_payloads(**kwargs) -> list[dict]:
    """Run post_chunks with the HTTP POST stubbed; return the payloads sent."""
    sent: list[dict] = []

    def fake_post(url, payload, **_):
        sent.append(payload)
        return MagicMock(json=lambda: {"status": "ok"})

    with patch.object(emit.http_client, "post_chunk", side_effect=fake_post):
        emit.post_chunks("imessage", "sid", ["x"], mcp_url="http://x", title="t", **kwargs)
    return sent


def test_chat_id_raw_is_a_top_level_field():
    """chat_id_raw is wired as a TOP-LEVEL ingest field (not a meta key), so the
    server stores it in the chat_id_raw column for same-conversation grouping.

    This is the exact path the iMessage / Apple Mail heredocs use; the parameter
    was once removed (breaking both connectors with a TypeError) — pin it here so
    the unit path, not just the .sh contract test, covers it."""
    payloads = _posted_payloads(chat_id_raw="iMessage;-;chat42")
    assert payloads
    assert payloads[0]["chat_id_raw"] == "iMessage;-;chat42"
    # It must be a top-level key, never folded into meta.
    assert "chat_id_raw" not in payloads[0].get("meta", {})


def test_chat_id_raw_omitted_when_none():
    """When no chat_id_raw is given, the key is absent (not a null) — matching
    sources like Apple Notes / Reminders that have no conversation id."""
    payloads = _posted_payloads()
    assert payloads
    assert "chat_id_raw" not in payloads[0]
