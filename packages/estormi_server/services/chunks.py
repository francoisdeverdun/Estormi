"""Chunk-store helpers.

:func:`retrieve_chunk_texts` is the batched, best-effort Qdrant text fetch used
where chunk text is needed alongside SQLite metadata (chunk text lives in the
vector store, not SQLite). Callers: the WhatsApp chat auto-tagging sampler
(``services.whatsapp._sample_chat_text``) and the ``get_chunk`` MCP handler
(``api.mcp_rpc``).
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()


async def retrieve_chunk_texts(ids: list[str]) -> dict[str, str]:
    """Return ``{id: text}`` for the given chunk ids, fetched from Qdrant.

    Chunk text lives in the vector store, not SQLite. Retrieval is batched and
    best-effort: a failed batch contributes nothing rather than raising.
    """
    from estormi_server.storage.tools import COLLECTION, _client  # noqa: PLC0415

    out: dict[str, str] = {}
    for i in range(0, len(ids), 100):
        sub = ids[i : i + 100]
        try:
            points = await _client().retrieve(
                collection_name=COLLECTION,
                ids=sub,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                text = (p.payload or {}).get("text") or ""
                if text:
                    out[str(p.id)] = text
        except Exception as e:
            log.warning("chunk text retrieve error", error=str(e), exc_info=True)
    return out
