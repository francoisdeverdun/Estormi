"""Regression: interrupted write spans can never wedge the shared connection.

On 2026-06-12 a writer interrupted between ``execute`` and ``commit`` left the
long-lived shared connection inside an open write transaction for over an
hour — every other writer on the file (including external briefing runs)
failed with ``database is locked`` until the app was restarted. Two defences
pin that bug shut:

* ``tools.write_txn()`` — the canonical leaf-writer span — rolls back on ANY
  abnormal exit, exceptions and task cancellation alike;
* ``tools.heal_orphaned_write_txn()`` — the scheduler watchdog — rolls back a
  transaction left open outside any locked span.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from estormi_server.storage import tools

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _fresh_db_and_lock():
    """In-memory DB as the shared connection + a fresh per-loop write lock."""
    saved_db, saved_lock = tools._db, tools._write_lock
    tools._db = await aiosqlite.connect(":memory:")
    await tools._db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    await tools._db.commit()
    tools._write_lock = asyncio.Lock()
    try:
        yield
    finally:
        await tools._db.close()
        tools._db, tools._write_lock = saved_db, saved_lock


async def _count() -> int:
    cur = await tools._db.execute("SELECT COUNT(*) FROM t")
    row = await cur.fetchone()
    await cur.close()
    return int(row[0])


class TestWriteTxn:
    async def test_commits_on_success(self):
        async with tools.write_txn() as db:
            await db.execute("INSERT INTO t VALUES ('a', '1')")
        assert not tools._db.in_transaction
        assert await _count() == 1

    async def test_rolls_back_on_exception(self):
        with pytest.raises(RuntimeError):
            async with tools.write_txn() as db:
                await db.execute("INSERT INTO t VALUES ('a', '1')")
                raise RuntimeError("boom")
        # The span's write is gone AND the connection is out of transaction —
        # the next writer is not blocked and cannot flush a half-written span.
        assert not tools._db.in_transaction
        assert await _count() == 0

    async def test_rolls_back_on_cancellation(self):
        started = asyncio.Event()

        async def _writer() -> None:
            async with tools.write_txn() as db:
                await db.execute("INSERT INTO t VALUES ('a', '1')")
                started.set()
                await asyncio.sleep(30)  # cancelled here, mid-span

        task = asyncio.create_task(_writer())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The cancellation rolled the span back and released the lock.
        assert not tools._db.in_transaction
        assert not tools._write_lock.locked()
        assert await _count() == 0


class TestOrphanWatchdog:
    async def test_heals_an_orphaned_transaction(self):
        # Simulate the wedge: a write executed with no commit, no lock held.
        await tools._db.execute("INSERT INTO t VALUES ('a', '1')")
        assert tools._db.in_transaction
        assert await tools.heal_orphaned_write_txn() is True
        assert not tools._db.in_transaction
        assert await _count() == 0

    async def test_leaves_a_live_writer_alone(self):
        # A writer legitimately mid-span holds the lock — the watchdog must
        # not roll its pending work back.
        async with tools._write_lock:
            await tools._db.execute("INSERT INTO t VALUES ('a', '1')")
            assert await tools.heal_orphaned_write_txn() is False
            assert tools._db.in_transaction
            await tools._db.commit()
        assert await _count() == 1

    async def test_noop_without_transaction(self):
        assert await tools.heal_orphaned_write_txn() is False
