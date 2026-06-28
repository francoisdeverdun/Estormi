"""End-to-end PII redaction through ``ingest_chunk``.

The shared ``filter_pii`` is well-unit-tested in
``tests/estormi_ingestion/test_ingestion.py::TestFilterPii``, but those tests only check the
function in isolation. They don't prove that:

  1. ``ingest_chunk`` actually invokes the filter when ``meta.pii_filtered``
     is absent / false, and
  2. the payload that lands in Qdrant contains the redacted text — not the
     raw PII the client posted.

Text content for chunks lives in the Qdrant point payload, not in the
SQLite ``chunks`` row (the SQL row is just metadata + ids). A regression
on either step above would silently store cleartext PII in the vector
store while every ``filter_pii`` unit test stays green. This integration
test reads back the upsert call to close that gap.

The ``meta.pii_filtered=True`` opt-out is exercised here too — connectors
that already ran the filter trust the server to skip the second pass, and
the text should land verbatim. Without that opt-out a refactor could
silently double-tag every chunk (``[REDACTED:[REDACTED:EMAIL]]``).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _last_upsert_text(mock_qdrant) -> str:
    """Return the ``text`` field of the most recent Qdrant upsert payload."""
    assert mock_qdrant.upsert.await_count >= 1, "ingest_chunk did not upsert"
    call = mock_qdrant.upsert.await_args
    points = call.kwargs.get("points") or call.args[0]
    assert points, "upsert called with no points"
    return points[-1].payload["text"]


async def test_email_phone_iban_redacted_in_stored_chunk(wired_tools_db, mock_qdrant):
    """A chunk with several PII types is upserted with markers, not raw values."""
    from estormi_server.storage import writers

    raw = (
        "Contact alice@example.com or +33 6 12 34 56 78 about "
        "FR76 3000 4000 5000 6000 7000 894. password: hunter2"
    )
    result = await writers.ingest_chunk(
        text=raw,
        source="docs",
        content_hash="pii-1",
        source_id="pii-doc",
    )
    assert result["status"] == "ok", result

    stored = _last_upsert_text(mock_qdrant)

    # The raw PII must not be present.
    assert "alice@example.com" not in stored
    assert "+33 6 12 34 56 78" not in stored
    assert "FR76 3000 4000 5000 6000 7000 894" not in stored
    assert "hunter2" not in stored

    # Redaction markers prove the server-side pass ran.
    assert "[REDACTED:EMAIL]" in stored
    assert "[REDACTED:PHONE_FR]" in stored
    assert "[REDACTED:FRENCH_IBAN]" in stored
    assert "[REDACTED:PASSWORD_LIKE]" in stored


async def test_otp_only_message_is_dropped(wired_tools_db, mock_qdrant):
    """An OTP/2FA notification is rejected outright, not stored redacted."""
    from estormi_server.storage import writers

    result = await writers.ingest_chunk(
        text="Your verification code is 482913 — do not share with anyone.",
        source="imessage",
        content_hash="otp-1",
        source_id="msg-otp",
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "otp_message"

    # Nothing should have been upserted to Qdrant or written to SQLite.
    assert mock_qdrant.upsert.await_count == 0
    cur = await wired_tools_db.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE content_hash = ?", ("otp-1",)
    )
    n = (await cur.fetchone())["n"]
    await cur.close()
    assert n == 0, "OTP-only messages must not land in the chunk store"


async def test_pii_filtered_meta_skips_server_pass(wired_tools_db, mock_qdrant):
    """``meta.pii_filtered=True`` is the connector contract — store verbatim."""
    from estormi_server.storage import writers

    # Already-redacted text. The server must NOT redact a second time, or
    # the markers themselves would get matched (e.g. ``password_like``
    # would re-eat ``[REDACTED:EMAIL]`` after a colon).
    text = "Already-cleaned chunk: [REDACTED:EMAIL] and [REDACTED:PHONE_FR]."
    result = await writers.ingest_chunk(
        text=text,
        source="docs",
        content_hash="meta-1",
        source_id="meta-doc",
        meta={"pii_filtered": True},
    )
    assert result["status"] == "ok"

    stored = _last_upsert_text(mock_qdrant)
    assert stored == text, "pii_filtered chunk must be stored verbatim"
