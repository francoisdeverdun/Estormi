"""Unit tests for WhatsApp sub-window slicing.

`message_sub_windows` slides a `chunk_msgs`-sized window over a conversation
window in `step`-message strides. Without the end-of-window break it used to
emit a final sub-window that was entirely contained inside the previous one —
pure duplicate work for the embedder/LLM.
"""

from __future__ import annotations

import pytest

from estormi_ingestion.whatsapp.ingest_conversations import message_sub_windows

pytestmark = pytest.mark.unit


def _msgs(n: int) -> list[dict]:
    return [{"i": i} for i in range(n)]


def test_short_window_returned_as_single_chunk():
    assert message_sub_windows(_msgs(8), 12, 6) == [_msgs(8)]
    # The boundary case (exactly chunk_msgs) is the same path.
    assert message_sub_windows(_msgs(12), 12, 6) == [_msgs(12)]


def test_no_duplicate_tail_sub_window():
    # 30 messages, chunk=12, step=6: starts at 0,6,12,18. A 5th start at 24
    # would be the tail msgs[24:30] = 6 msgs, all already inside msgs[18:30].
    result = message_sub_windows(_msgs(30), 12, 6)
    assert [w[0]["i"] for w in result] == [0, 6, 12, 18]
    assert all(len(w) == 12 for w in result)


def test_partial_tail_chunk_is_emitted_when_it_carries_new_messages():
    # 25 messages, chunk=12, step=6: starts at 0,6,12,18. msgs[18:25] is the
    # last 7 messages — msg 24 isn't covered by the previous window.
    result = message_sub_windows(_msgs(25), 12, 6)
    assert [w[0]["i"] for w in result] == [0, 6, 12, 18]
    assert [len(w) for w in result] == [12, 12, 12, 7]


def test_full_step_progression_terminates_cleanly():
    result = message_sub_windows(_msgs(100), 12, 6)
    starts = [w[0]["i"] for w in result]
    # Every step is exactly 6; the last window starts so that it covers
    # the final message exactly once.
    assert starts == [0, 6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72, 78, 84, 90]
    # The last window may be shorter when the tail doesn't fill chunk_msgs.
    assert len(result[-1]) == 10


def test_display_name_resolves_unknown_dm_sender(monkeypatch):
    """A 1:1 DM whose sender is '[unknown]' or a bare number resolves to the
    chat partner's name; groups keep '[unknown]' (the briefing filters it), and
    a real sender name passes through."""
    from estormi_ingestion.whatsapp import ingest_conversations as ic

    monkeypatch.setitem(ic._chat_name_cache, "33600000000@s.whatsapp.net", "Alice Martin")

    assert ic._display_name("unknown", "33600000000@s.whatsapp.net") == "Alice Martin"
    assert ic._display_name("33600000000", "33600000000@s.whatsapp.net") == "Alice Martin"
    assert ic._display_name("unknown", "120363@g.us") == "unknown"
    assert ic._display_name("Tristan", "120363@g.us") == "Tristan"
