"""Two-store chunk writers: the SQLite + Qdrant mutation paths.

Owns the write side of storage — :func:`ingest_chunk` (embed + dual-store
insert with crash-safe rollback) and the delete/update helpers. These were
the bulk of the old ``tools`` god-module; they live here so ``tools`` is left
holding only the shared connection state and the config.

The shared connection (:func:`tools.sqlite_conn`), the Qdrant client
(:func:`tools._client`), the embedding functions, the collection name and the
write serialiser (:data:`tools._write_lock`) all stay on :mod:`tools` because
the lifespan and the test suite swap them by attribute
(``tools._db = …``, ``patch("estormi_server.storage.tools._client", …)``). This module
reaches every one of them through a lazy ``from estormi_server.storage import tools``
so those swaps and patches keep working transparently — the same idiom
``chunk_admin`` / ``qdrant_helpers`` / ``search_api`` use.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import aiosqlite
import structlog
from qdrant_client.models import PointIdsList, PointStruct, SparseVector

from memory_core.audit import log_tool_call
from memory_core.timeparse import parse_iso

log = structlog.get_logger(__name__)


# Sources whose chunks are *world* knowledge (news / RSS / video) rather than
# the user's personal memory. Everything else — mail, calendar, chats, docs,
# the daily briefing — is `personal`. The tag lets retrieval scope so a
# personal query never surfaces world noise, while the briefing can deliberately
# merge the two.
# `knowledge` is the connector/source name the world ingester writes under (see
# estormi_ingestion/knowledge/ingest_world.py); `news`/`rss`/`youtube` are legacy labels
# kept so chunks ingested before the rename still derive `world`.
WORLD_SOURCES: frozenset[str] = frozenset({"knowledge", "news", "rss", "youtube"})

# Hard ceiling on a single chunk's text — generous vs the embedding context
# window, low enough that one abusive call can't drive SQLite+Qdrant into RAM
# exhaustion. The REST shim (api/ingest.IngestBody) enforces the same value via
# Pydantic; ingest_chunk re-checks it so the MCP tools/call path (which skips
# Pydantic) shares the one ceiling.
MAX_TEXT_LEN = 1_000_000


def _corpus_for_source(source: str) -> str:
    """Map a source name to its corpus (`world` | `personal`)."""
    return "world" if (source or "").lower() in WORLD_SOURCES else "personal"


def _chat_kind_from_jid(chat_id: str) -> str:
    """Structural WhatsApp chat kind (dm/group/broadcast) from the JID suffix.

    This is the *structural* axis, stored in the ``chat_kind`` column — distinct
    from the *semantic* ``group_type`` tag (work/family/…). Keep the suffix→kind
    mapping in sync with the SQLite backfill in ``sql.schema.MIGRATION_SQL``.
    """
    if chat_id.endswith("@g.us"):
        return "group"
    # @lid is WhatsApp's phone-number-hiding identity for individuals — a DM,
    # not a group, just one whose contact reached you without sharing a number.
    if chat_id.endswith(("@s.whatsapp.net", "@lid")):
        return "dm"
    if chat_id.endswith("@broadcast"):
        return "broadcast"
    return "unknown"


async def _resolve_stale_ids(db, source: str, source_id: str, content_hash: str) -> list[str]:
    """Read-only: ids of prior rows for ``(source, source_id)`` to retire.

    Returns the ids of existing rows whose *base* content_hash (the part before
    any ``-<n>`` chunk-index suffix) differs from the incoming chunk's — i.e. the
    upstream item changed and its old chunks should be replaced. This only
    *reads*; the actual SQLite + Qdrant deletes are deferred until after the new
    row is durable (see ``ingest_chunk``'s crash-safe ordering). Empty when
    ``source_id`` is blank or nothing matches.
    """
    if not source_id:
        return []
    current_base = content_hash.rsplit("-", 1)[0] if "-" in content_hash else content_hash
    cursor = await db.execute(
        "SELECT id, content_hash FROM chunks WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    old_rows = await cursor.fetchall()
    await cursor.close()
    return [
        r["id"]
        for r in old_rows
        if (r["content_hash"].rsplit("-", 1)[0] if "-" in r["content_hash"] else r["content_hash"])
        != current_base
    ]


def _collect_extra_payload(
    *,
    group_type: str | None,
    chat_kind: str | None,
    pending_reply: bool,
    chat_id_raw: str | None,
    event_type: str | None,
    event_status: str | None,
    working_location: str | None,
) -> dict:
    """Assemble the optional Qdrant payload keys, omitting unset ones.

    Only truthy fields land in the payload so the vector store stays free of
    empty/None keys (matching the SQLite NULL-collapse for the same fields).
    """
    extra: dict = {}
    if group_type:
        extra["group_type"] = group_type
    if chat_kind:
        extra["chat_kind"] = chat_kind
    if pending_reply:
        extra["pending_reply"] = pending_reply
    if chat_id_raw:
        extra["chat_id_raw"] = chat_id_raw
    if event_type:
        extra["event_type"] = event_type
    if event_status:
        extra["event_status"] = event_status
    if working_location:
        extra["working_location"] = working_location
    return extra


async def _rollback_dual_store(db, tools, point_id: str) -> None:
    """Undo a partial two-store write: SQLite rollback then Qdrant point delete.

    The shared connection runs in deferred-transaction mode, so a pending INSERT
    must be rolled back first or the next successful caller's commit flushes it,
    leaving an orphan SQLite row with no vector. The Qdrant delete then removes
    the already-upserted point so we don't leak an orphan the other way. Both
    steps are best-effort — failures are logged, never raised — so the caller's
    original exception (or duplicate-resolution) still drives control flow.
    """
    try:
        await db.rollback()
    except Exception:
        log.exception("ingest_chunk.sqlite_rollback_failed", point_id=point_id)
    try:
        await tools._client().delete(
            collection_name=tools.COLLECTION,
            points_selector=PointIdsList(points=[point_id]),
        )
    except Exception:
        log.exception("ingest_chunk.qdrant_rollback_failed", point_id=point_id)


async def ingest_chunk(
    text: str,
    source: str,
    title: str = "",
    date: str = "",
    url: str = "",
    content_hash: str = "",
    source_id: str = "",
    group_type: str | None = None,
    pending_reply: bool = False,
    chat_id_raw: str | None = None,
    chat_name: str | None = None,
    end_date_ts: str | None = None,
    corpus: str | None = None,
    event_type: str | None = None,
    event_status: str | None = None,
    working_location: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Embed + store a chunk. Update semantics:

    - If a row with the same `content_hash` exists → skip (idempotent).
    - Else if `source_id` is given and already exists → delete old rows then insert.
    - Else → fresh insert.
    """
    from estormi_server.storage import tools  # noqa: PLC0415

    meta = meta or {}
    if corpus is None:
        corpus = _corpus_for_source(source)
    t0 = time.time()

    if not text.strip():
        return {"status": "skipped", "reason": "empty_text"}
    if len(text) > MAX_TEXT_LEN:
        # Backstop for the MCP path, which bypasses the REST shim's Pydantic cap.
        return {"status": "error", "reason": "text_too_large"}
    if not content_hash:
        return {"status": "error", "reason": "missing_content_hash"}

    # Server-side PII enforcement. Connectors are expected to filter before
    # POSTing, but we treat that as advisory: re-run the filter unless the
    # caller explicitly tells us the chunk was already scrubbed
    # (``meta["pii_filtered"] is True``). OTP/2FA notifications are dropped
    # entirely — they have no archival value and would just leak codes.
    if not meta.get("pii_filtered"):
        if tools._is_otp_message(text):
            log.debug("ingest_chunk.dropped_otp", source=source, source_id=source_id)
            return {"status": "skipped", "reason": "otp_message"}
        filtered = tools._filter_pii(text)
        if filtered != text:
            log.debug("ingest_chunk.pii_redacted", source=source, source_id=source_id)
            text = filtered

    # Titles bypass the body filter above: connectors send a pre-filtered body
    # with ``pii_filtered:True`` but a RAW title (email subjects, calendar
    # titles, note first-lines), so a secret in the title would land raw in the
    # Qdrant payload, the SQLite row, and the audit log. Redact unconditionally
    # — titles are small, so this defence-in-depth is cheap.
    if title:
        title = tools._filter_pii(title)

    # If Qdrant was locked at startup (qdrant.locked_at_startup), the
    # collection schema was never created. The helper short-circuits when
    # already ready.
    await tools._ensure_collection_ready()

    db = tools.sqlite_conn()

    # Fast-path dedupe: a best-effort read OUTSIDE the write lock so the common
    # "already ingested" case returns without serialising. It is NOT the dedupe
    # guarantee — a concurrent insert can race between this SELECT and the locked
    # INSERT; the UNIQUE(content_hash) constraint + the IntegrityError handler
    # below are what actually enforce single-copy. Don't move it under the lock.
    cursor = await db.execute("SELECT id FROM chunks WHERE content_hash = ?", (content_hash,))
    existing = await cursor.fetchone()
    await cursor.close()
    if existing:
        return {"status": "skipped", "reason": "duplicate", "id": str(existing["id"])}

    dense_vec = await tools.embed_one(text)
    sparse_vec = await tools.sparse_embed_one(text)
    point_id = str(uuid.uuid4())

    date_ts_iso: str | None = None
    parsed = parse_iso(date)
    if parsed:
        date_ts_iso = parsed.isoformat()

    # group_type is the *semantic* life-context tag (work/family/…); it is no
    # longer auto-derived from the JID. The *structural* kind (dm/group/
    # broadcast) lives in its own field, derived from the JID for WhatsApp.
    chat_kind = _chat_kind_from_jid(chat_id_raw) if chat_id_raw else None

    # Empty strings (a connector sending "" for "no working location") collapse
    # to NULL so the column means exactly "set or not", and the Qdrant payload
    # stays free of empty keys.
    event_type = event_type or None
    event_status = event_status or None
    working_location = working_location or None

    extra_payload = _collect_extra_payload(
        group_type=group_type,
        chat_kind=chat_kind,
        pending_reply=pending_reply,
        chat_id_raw=chat_id_raw,
        event_type=event_type,
        event_status=event_status,
        working_location=working_location,
    )

    # Normalize end_date_ts to a UTC isoformat string if provided
    end_date_ts_iso: str | None = None
    if end_date_ts:
        parsed_end = parse_iso(end_date_ts)
        if parsed_end:
            end_date_ts_iso = parsed_end.isoformat()

    # Serialise the two-store write span (Qdrant upsert → SQLite INSERT →
    # commit) on the shared connection: another writer's commit must not flush
    # our still-pending INSERT mid-flight. Embedding above ran lock-free so the
    # slow path doesn't block other writers. See ``tools._write_lock``.
    async with tools._write_lock:
        # Identify rows we may need to retire after the new chunk lands —
        # resolved UNDER the lock, because a concurrent writer for the same
        # (source, source_id) may commit new rows while we embed, and a
        # pre-lock snapshot would miss or wrongly retire them. It's one local
        # SQLite read, negligible next to the embed kept outside the lock. We
        # only *read* here — the actual SQLite + Qdrant deletes are deferred
        # until after the new row is durable. The previous implementation
        # deleted stale rows AND their Qdrant points before embedding/inserting
        # the new content, so a crash, embed failure, or process kill between
        # the delete and the new INSERT silently destroyed the chunk with no
        # replacement.
        stale_ids = await _resolve_stale_ids(db, source, source_id, content_hash)

        await tools._client().upsert(
            collection_name=tools.COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector={
                        tools.DENSE_VECTOR_NAME: dense_vec,
                        tools.SPARSE_VECTOR_NAME: SparseVector(
                            indices=sparse_vec["indices"],
                            values=sparse_vec["values"],
                        ),
                    },
                    payload={
                        "text": text,
                        "source": source,
                        "source_id": source_id or None,
                        "title": title,
                        "date": date,
                        "date_ts": date_ts_iso,
                        "url": url,
                        "content_hash": content_hash,
                        "corpus": corpus,
                        "ingested_at": datetime.now(timezone.utc).isoformat(),
                        **extra_payload,
                    },
                )
            ],
        )

        # From here on, the Qdrant point is already in the vector store. Any
        # exception from the SQL writes below (IntegrityError on the INSERT, a
        # commit-time failure from disk-full / lock contention, the optional
        # whatsapp upsert, or the stale_ids cleanup) leaves an orphaned vector
        # with no SQL backing — unrecoverable through normal queries. Catch every
        # failure path so we can roll the vector back before re-raising.
        try:
            try:
                await db.execute(
                    """
                    INSERT INTO chunks
                        (id, content_hash, source, source_id, title, date, date_ts,
                         end_date_ts, group_type, pending_reply, chat_id_raw, corpus,
                         event_type, event_status, working_location, chat_kind, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        point_id,
                        content_hash,
                        source,
                        source_id or None,
                        title,
                        date,
                        date_ts_iso,
                        end_date_ts_iso,
                        group_type,
                        1 if pending_reply else 0,
                        chat_id_raw,
                        corpus,
                        event_type,
                        event_status,
                        working_location,
                        chat_kind,
                    ),
                )
            except aiosqlite.IntegrityError:
                # Race: a concurrent ingester won the UNIQUE(content_hash) check
                # between our SELECT above and this INSERT. Rather than surface a
                # 500, roll back our Qdrant point and report a clean "duplicate".
                # Roll back first so the failed INSERT (and any open read tx) is
                # discarded rather than left pending for the next caller's commit.
                await _rollback_dual_store(db, tools, point_id)
                cursor = await db.execute(
                    "SELECT id FROM chunks WHERE content_hash = ?",
                    (content_hash,),
                )
                winner = await cursor.fetchone()
                await cursor.close()
                return {
                    "status": "skipped",
                    "reason": "duplicate",
                    "id": str(winner["id"]) if winner else None,
                }
            if chat_id_raw and source == "whatsapp":
                await db.execute(
                    """
                    INSERT INTO whatsapp_chats (chat_id, chat_name, group_type, chat_kind)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        chat_name = CASE WHEN excluded.chat_name != '' THEN excluded.chat_name ELSE chat_name END,
                        chat_kind = COALESCE(excluded.chat_kind, chat_kind),
                        last_seen = datetime('now')
                    """,
                    (
                        chat_id_raw,
                        chat_name or "",
                        # Semantic tag only — defaults to 'unknown' until the user or
                        # the auto-tagger categorises the chat. The structural kind
                        # goes in its own column.
                        group_type or "unknown",
                        chat_kind,
                    ),
                )

            # Now that the new row's INSERT has succeeded (still uncommitted),
            # retire the stale rows in the same transaction. Either both the new
            # row + the old-row deletes commit together, or neither does — no
            # partial-write window where the chunk is gone with no replacement.
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                await db.execute(
                    f"DELETE FROM chunks WHERE id IN ({placeholders})",
                    stale_ids,
                )
            await db.commit()
        except BaseException:
            # Anything from the INSERT-onwards failed AFTER we wrote to Qdrant
            # — including task cancellation (an uncaught CancelledError here
            # would leave the shared connection stuck inside the open write
            # transaction, wedging every other writer on the file). Roll the
            # SQLite tx and the Qdrant point back (best-effort) so we leave no
            # orphan on either side, then re-raise the original error.
            await _rollback_dual_store(db, tools, point_id)
            raise

        # SQLite is now consistent. Drop the stale Qdrant points last — a crash
        # here leaves orphaned vectors but they have no SQL backing, so search
        # never surfaces them and an admin pass can sweep them up. Best-effort
        # only; failure here does not roll back the SQL commit.
        if stale_ids:
            try:
                await tools._client().delete(
                    collection_name=tools.COLLECTION,
                    # str ids are valid ExtendedPointId; the qdrant stub's List is
                    # invariant so list[str] doesn't match — narrow for the checker.
                    points_selector=PointIdsList(points=list(stale_ids)),  # pyright: ignore[reportArgumentType]
                )
            except Exception:
                log.exception(
                    "ingest_chunk.qdrant_stale_delete_failed",
                    source=source,
                    source_id=source_id,
                )

    # No per-user JWT on the loopback transport, so there is no caller identity
    # to record — the audit row always carries the "unknown" sentinel.
    log_tool_call(
        token_sub="unknown",
        token_email="unknown",
        tool_name="ingest_chunk",
        query=f"[ingest:{source}] {title}",
        result_ids=[point_id],
        duration_ms=(time.time() - t0) * 1000,
    )
    result: dict[str, object] = {"status": "ok", "id": point_id}
    if stale_ids:
        result["replaced"] = len(stale_ids)
    return result


async def delete_chunk(chunk_id: str) -> dict:
    """Delete a single chunk from SQLite + Qdrant.

    Qdrant-first ordering: vector deletion is idempotent (no-op on missing id)
    so a crash between the two writes leaves no orphan — the SQL row is still
    there as a retry signal. The reverse order would leave a vector with no
    SQL backing — a true orphan with no path to discovery.
    """
    from estormi_server.storage import tools  # noqa: PLC0415

    db = tools.sqlite_conn()
    cursor = await db.execute("SELECT id FROM chunks WHERE id = ?", (chunk_id,))
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        return {"status": "not_found", "id": chunk_id}
    # Serialise the Qdrant delete → SQLite delete → commit span on the shared
    # connection so it can't interleave with another writer's commit; the
    # write_txn guard rolls back if the span is interrupted mid-flight.
    async with tools.write_txn():
        await tools._client().delete(
            collection_name=tools.COLLECTION,
            points_selector=PointIdsList(points=[chunk_id]),
        )
        await db.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
    log_tool_call(
        token_sub="unknown",
        token_email="unknown",
        tool_name="delete_chunk",
        query=f"[delete] {chunk_id}",
        result_ids=[chunk_id],
        duration_ms=0,
    )
    return {"status": "ok", "deleted": 1, "id": chunk_id}


async def delete_by_source_id(source: str, source_id: str) -> dict:
    """Delete every chunk for one (source, source_id) from SQLite + Qdrant.

    Used by connectors that need to retract a single item — e.g. the
    Google Calendar sync deleting an event the user cancelled. Qdrant-first
    ordering — see ``delete_chunk`` for the rationale.
    """
    from estormi_server.storage import tools  # noqa: PLC0415

    db = tools.sqlite_conn()
    # Read the ids and both deletes under the write lock so an ``ingest_chunk``
    # committing in between can't slip in a row+vector the predicate DELETE would
    # drop from SQLite while its vector stays in Qdrant (orphan). write_txn also
    # rolls back if the span is interrupted mid-flight.
    async with tools.write_txn():
        cursor = await db.execute(
            "SELECT id FROM chunks WHERE source = ? AND source_id = ?", (source, source_id)
        )
        ids = [row["id"] for row in await cursor.fetchall()]
        await cursor.close()
        if not ids:
            return {"status": "not_found", "deleted": 0}
        await tools._client().delete(
            collection_name=tools.COLLECTION,
            points_selector=PointIdsList(points=ids),
        )
        await db.execute(
            "DELETE FROM chunks WHERE source = ? AND source_id = ?", (source, source_id)
        )
    log_tool_call(
        token_sub="unknown",
        token_email="unknown",
        tool_name="delete_by_source_id",
        query=f"[delete:{source}] {source_id}",
        result_ids=ids,
        duration_ms=0,
    )
    return {"status": "ok", "deleted": len(ids)}


async def delete_by_source(source: str) -> dict:
    """Admin helper — wipe all chunks from a given source.

    Qdrant-first ordering — see ``delete_chunk`` for the rationale.
    """
    from estormi_server.storage import tools  # noqa: PLC0415

    db = tools.sqlite_conn()
    # Read the ids and both deletes under the write lock — see
    # ``delete_by_source_id`` for the orphan-vector race this closes.
    async with tools.write_txn():
        cursor = await db.execute("SELECT id FROM chunks WHERE source = ?", (source,))
        ids = [r["id"] for r in await cursor.fetchall()]
        await cursor.close()
        if ids:
            await tools._client().delete(
                collection_name=tools.COLLECTION,
                points_selector=PointIdsList(points=ids),
            )
        await db.execute("DELETE FROM chunks WHERE source = ?", (source,))
    log_tool_call(
        token_sub="unknown",
        token_email="unknown",
        tool_name="delete_by_source",
        query=f"[delete:{source}] *",
        result_ids=ids,
        duration_ms=0,
    )
    return {"status": "ok", "deleted": len(ids), "source": source}
