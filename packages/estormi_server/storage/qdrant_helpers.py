"""Qdrant collection setup + payload-rendering helpers.

Owns :func:`ensure_collection` (used by the lifespan and by callers that
need to repair after a Qdrant lock at startup) plus the small helpers that
render a Qdrant payload into a search-result text. The shared Qdrant
client (:func:`tools._client`) and the collection name (:data:`tools.COLLECTION`)
stay in :mod:`tools` because the test suite patches them as attributes
of that module; this module reaches them via ``tools.<name>`` so those
patches keep working.
"""

from __future__ import annotations

import structlog
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    SparseVectorParams,
    VectorParams,
)

from memory_core.embedder import EMBED_DIM
from memory_core.sanitizer import sanitize_chunk

log = structlog.get_logger(__name__)


def _search_result_text(payload: dict) -> str:
    return sanitize_chunk(payload.get("text", ""))


async def ensure_collection(recreate: bool = False) -> None:
    """Create (or recreate) the Qdrant collection with dense+sparse named vectors.

    Validates that an existing collection's dense-vector size matches
    :data:`EMBED_DIM` — if the operator changed ``EMBED_MODEL`` to a model
    with a different output width without recreating the collection, every
    subsequent upsert would fail. Raise loudly here with actionable text.
    """
    # Late-binding through ``tools`` so the test suite's ``patch("estormi_server.storage.tools._client", ...)``
    # affects this caller too. ``COLLECTION`` and the vector names are
    # attributes of ``tools`` for the same reason.
    from estormi_server.storage import tools  # noqa: PLC0415

    client = tools._client()
    existing = {c.name for c in (await client.get_collections()).collections}

    if recreate and tools.COLLECTION in existing:
        await client.delete_collection(collection_name=tools.COLLECTION)
        existing.discard(tools.COLLECTION)

    if tools.COLLECTION not in existing:
        await client.create_collection(
            collection_name=tools.COLLECTION,
            vectors_config={
                tools.DENSE_VECTOR_NAME: VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                tools.SPARSE_VECTOR_NAME: SparseVectorParams(),
            },
        )
    else:
        # Existing collection: confirm the dense-vector size matches
        # EMBED_DIM. A silent mismatch causes every ingest to fail at upsert
        # time with an opaque error — surface the discrepancy at startup
        # where the operator can fix EMBED_DIM / EMBED_MODEL / recreate.
        try:
            info = await client.get_collection(collection_name=tools.COLLECTION)
            vectors_cfg = info.config.params.vectors
            dense_cfg = (
                vectors_cfg.get(tools.DENSE_VECTOR_NAME) if isinstance(vectors_cfg, dict) else None
            )
            if dense_cfg is not None and getattr(dense_cfg, "size", None) not in (
                None,
                EMBED_DIM,
            ):
                raise RuntimeError(
                    f"Qdrant collection {tools.COLLECTION!r} has dense vector "
                    f"size {dense_cfg.size}, but EMBED_DIM={EMBED_DIM}. "
                    f"Either set EMBED_DIM={dense_cfg.size} (matching the "
                    f"existing collection) or run an admin reset to rebuild."
                )
        except RuntimeError:
            raise
        except Exception:
            # Don't fail startup just because we couldn't introspect — the
            # mismatch (if any) will surface on the first upsert, and the
            # error message there now points at this check too. Log it (not a
            # warning: introspection failing is expected on a fresh collection)
            # so a real permission/connection fault isn't completely silent.
            log.info("ensure_collection.introspect_skipped", exc_info=True)

    # NB: under the shipped EMBEDDED (path-based) Qdrant these payload indexes
    # are inert — local Qdrant ignores them (it emits "Payload indexes have no
    # effect in the local Qdrant"), so search_memory's source/corpus/date
    # filters scan the full payload population rather than an index. That is a
    # latency concern on a large corpus, NOT a correctness one (filtering still
    # returns the right chunks), and the time-window fetch_around path is served
    # by a real SQLite expression index. These calls are kept for parity with a
    # future server-mode Qdrant deployment, where they DO take effect.
    for field, schema in [
        ("source", PayloadSchemaType.KEYWORD),
        ("source_id", PayloadSchemaType.KEYWORD),
        ("content_hash", PayloadSchemaType.KEYWORD),
        ("date_ts", PayloadSchemaType.DATETIME),
        ("group_type", PayloadSchemaType.KEYWORD),
        ("chat_kind", PayloadSchemaType.KEYWORD),
        ("pending_reply", PayloadSchemaType.BOOL),
        ("corpus", PayloadSchemaType.KEYWORD),
    ]:
        try:
            await client.create_payload_index(
                collection_name=tools.COLLECTION,
                field_name=field,
                field_schema=schema,
            )
        except Exception:
            # Best-effort: the index usually already exists on this field.
            log.info("ensure_collection.payload_index_skipped", field=field, exc_info=True)
