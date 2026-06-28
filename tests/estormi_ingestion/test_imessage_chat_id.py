"""iMessage chunks carry a ``chat_id_raw`` column so messages from one
conversation can be grouped together during time-window retrieval
(``fetch_around``). WhatsApp already sets it; iMessage previously did not, so its
chunks could not be grouped.

The iMessage ingest logic used to live inside a shell heredoc in
``watch_and_ingest.sh`` and could not be imported, so this test once read the
script as text and asserted a ``chat_id_raw=chat_id,`` substring. The body is now
an importable module (``estormi_ingestion.imessage.ingest``), so this drives the
real ``main()`` with the HTTP POST stubbed and asserts the actual ``/ingest_chunk``
payload wires ``chat_id_raw`` to the chat id — the contract a ``post_chunks``
``TypeError`` once broke.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from estormi_ingestion.imessage import ingest as imessage_ingest
from estormi_ingestion.shared import emit

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_imessage_ingest_payload_carries_chat_id_raw(tmp_path):
    """The iMessage /ingest_chunk payload must wire chat_id_raw to the chat id.

    ``chat_id_raw`` is a typed *top-level* parameter of ``post_chunks`` (see
    ``estormi_ingestion/shared/emit.py``), NOT a ``meta`` key or a generic
    ``extra`` dict. Driving the real module proves the keyword wiring at runtime;
    the signature contract is enforced separately by
    ``tests/contract/test_shell_emit_call_sites.py``.
    """
    meta = tmp_path / "m.meta.json"
    body = tmp_path / "m.txt"
    meta.write_text(
        json.dumps(
            {
                "id": "guid-1",
                "chat_id": "iMessage;-;chat42",
                "chat_name": "Family",
                "timestamp_iso": "2026-06-20T10:00:00Z",
            }
        )
    )
    body.write_text("hello there everyone")

    sent: list[dict] = []

    def fake_post(url, payload, **_):
        sent.append(payload)
        return MagicMock(json=lambda: {"status": "ok"})

    with patch.object(emit.http_client, "post_chunk", side_effect=fake_post):
        rc = imessage_ingest.main(["-", str(meta), str(body), "http://x/", "/repo", "800", "100"])

    assert rc == 0
    assert sent, "iMessage ingest posted no chunk"
    assert sent[0]["chat_id_raw"] == "iMessage;-;chat42", (
        "iMessage chunks must wire chat_id_raw=<chat id> through post_chunks so "
        "same-conversation chunks group together during fetch_around retrieval"
    )
    # It must be a top-level ingest field, never folded into meta.
    assert "chat_id_raw" not in sent[0].get("meta", {})
