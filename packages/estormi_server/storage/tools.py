"""Shared, mutable storage state for the Estormi storage + retrieval layer.

This module is the home of the globals every storage module reaches into by
attribute (late-bound, so the test suite's ``patch("estormi_server.storage.tools.<name>", …)``
hooks keep working):

* the shared ``_db`` aiosqlite connection — the lifespan and the test
  ``conftest.py`` mutate ``tools._db`` directly, so the global has to live
  here (not behind another package).
* the shared ``_qdrant`` client and its accessor ``_client``.
* the write serialiser ``_write_lock`` and the collection config
  (``COLLECTION``, ``DATA_DIR``, …).
* the embedding functions and the server-side PII filter, re-exported here
  because the read/write paths call them as ``tools.embed_one`` /
  ``tools._filter_pii``.

The behaviour that only *reaches* these globals lives in its own module and is
imported from there directly: :mod:`sql.schema`, :mod:`sql.connection`,
:mod:`qdrant_helpers`, :mod:`search_api`, :mod:`chunk_admin`, :mod:`writers`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
import structlog
from qdrant_client import AsyncQdrantClient

from estormi_server.storage.qdrant_helpers import ensure_collection
from memory_core import llm_local as _llm_local

# embed_one / sparse_embed_one are not used in this module directly, but the
# search and write paths reach them as ``tools.embed_one`` (late-bound) and the
# test suite patches ``estormi_server.storage.tools.embed_one`` — so they must stay
# importable as attributes of this module.
from memory_core.embedder import embed_one, sparse_embed_one  # noqa: F401
from memory_core.settings import DATA_DIR, DB_PATH  # noqa: F401

log = structlog.get_logger(__name__)


# Wire the PII filter so /api/ingest_chunk enforces redaction server-side
# (defence in depth: even if a connector forgot to filter, secrets never land in
# storage). It lives in memory_core — the always-importable bottom layer — so the
# import is unconditional: the server's last-line PII/OTP defence can no longer
# silently degrade to no-ops the way the old try/except fallback allowed.
from memory_core.pii_filter import filter_pii as _filter_pii  # noqa: E402,F401
from memory_core.pii_filter import is_otp_message as _is_otp_message  # noqa: E402,F401

COLLECTION = os.getenv("QDRANT_COLLECTION", "estormi")

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25"

# Shared DB connection — set by main.py lifespan. Never create per-call.
_db: aiosqlite.Connection | None = None

# Application-level write serialiser. The single shared ``_db`` connection is
# used by every coroutine, and a write transaction here spans multiple awaits
# (``db.execute(INSERT)`` → ``await qdrant.upsert`` → ``db.commit()``). Without
# a lock, two writers can interleave on that one connection — the second
# caller's ``commit()`` flushes the first caller's still-pending ``INSERT``,
# tearing the two-store (SQLite + Qdrant) writes apart. Every write path that
# owns its own execute→commit/rollback span acquires this before touching the
# connection. Leaf writers only — never acquire it re-entrantly (a function
# holding it must not call another that also acquires it), or aiosqlite's single
# connection deadlocks.
_write_lock: asyncio.Lock = asyncio.Lock()


def get_write_lock() -> asyncio.Lock:
    """Return the shared write serialiser (see ``_write_lock``).

    Callers that own a multi-statement write span across the shared connection
    but live outside this module (admin endpoints chaining their own
    ``execute``/``commit`` after a leaf writer) acquire this so they can't
    interleave with the leaf writers here. Do NOT acquire it while already
    holding it — the lock is non-reentrant. Prefer :func:`write_txn` for plain
    execute→commit spans: it adds the rollback-on-interruption guarantee.
    """
    return _write_lock


@asynccontextmanager
async def write_txn() -> AsyncIterator[aiosqlite.Connection]:
    """Serialised write span on the shared connection — the canonical leaf
    writer. Acquires the write lock, yields the connection, commits on normal
    exit, and rolls back on ANY abnormal exit — exceptions AND task
    cancellation (``BaseException``).

    The rollback guarantee is the point: a writer interrupted between
    ``execute`` and ``commit`` (a raised commit, a cancelled task at an await
    point) used to leave the long-lived shared connection stuck inside an open
    write transaction — every other writer on the file then failed with
    ``database is locked`` until the process restarted (observed 2026-06-12:
    a >1h wedge that killed external briefing runs).
    """
    async with _write_lock:
        db = sqlite_conn()
        try:
            yield db
            # commit() is INSIDE the try: a raised commit (disk-full, lock
            # contention) must hit the rollback below, exactly as the docstring
            # promises. With it in an ``else`` it escaped the guard and left the
            # connection wedged in an open transaction — the bug this docstring
            # describes was only half-fixed.
            await db.commit()
        except BaseException:
            try:
                await db.rollback()
            except Exception:
                log.exception("write_txn.rollback_failed")
            raise


async def heal_orphaned_write_txn() -> bool:
    """Watchdog: roll back a write transaction left open on the shared
    connection by an interrupted writer nobody guarded (see ``write_txn``).

    Returns True when an orphan was healed. The check is race-free: an open
    transaction while the write lock is FREE can only be an orphan — every
    legitimate writer holds the lock for its whole execute→commit span. Runs
    on a scheduler interval (lifespan) so a wedge clears in seconds instead
    of holding every other writer hostage until a restart.
    """
    db = _db
    if db is None or not db.in_transaction or _write_lock.locked():
        return False
    async with _write_lock:
        if not db.in_transaction:  # the owner committed while we waited
            return False
        log.error("db.orphaned_write_txn_rolled_back")
        await db.rollback()
        return True


_qdrant: AsyncQdrantClient | None = None

# Set True when ``ensure_collection`` has run successfully (either at lifespan
# startup or on the lazy-recovery path below). Search and ingest both call
# ``_ensure_collection_ready`` so a Qdrant lock that made startup bail
# (``qdrant.locked_at_startup``) is recovered transparently on first use.
_collection_ready: bool = False


async def _ensure_collection_ready() -> None:
    """Run ``ensure_collection`` once, lazily, if startup failed to do so.

    The lifespan handler tries ``ensure_collection`` at startup but swallows
    a Qdrant lock so the server still boots. Without this hook the first
    search after a locked startup would hit a non-existent collection and
    raise; now the lazy path catches up before the first read or write.
    """
    global _collection_ready
    if _collection_ready:
        return
    await ensure_collection()
    _collection_ready = True


def _client() -> AsyncQdrantClient:
    """Return the shared embedded-Qdrant client.

    The lifespan handler eagerly constructs this via ``ensure_collection()``
    at startup; the lazy branch below only fires as the recovery path when
    Qdrant was locked at startup (see ``qdrant.locked_at_startup``). The
    check-and-set is safe without a lock: this function is synchronous and is
    only ever called from the single event-loop thread, so no ``await`` point
    can interleave two callers between the ``None`` check and the assignment.
    """
    global _qdrant
    if _qdrant is None:
        _qdrant = AsyncQdrantClient(path=os.path.join(DATA_DIR, "qdrant"))
    return _qdrant


def sqlite_conn() -> aiosqlite.Connection:
    """Return the shared aiosqlite connection (set by main.py lifespan).
    SYNCHRONOUS — never await this call.
    Never use `async with conn:` on the returned connection — that closes it.
    For writes, call `await db.commit()` explicitly."""
    if _db is None:
        raise RuntimeError("DB not initialized — lifespan must have started")
    return _db


# Let memory_core read ``*_model_tier`` settings through the live shared
# connection instead of reaching up into this module (inverts the old
# ``llm_local -> tools`` import; see tests/contract/test_import_linter_layers.py).
_llm_local.set_settings_conn_provider(sqlite_conn)
