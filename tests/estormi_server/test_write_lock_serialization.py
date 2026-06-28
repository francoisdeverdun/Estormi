"""Regression: the shared write lock serialises concurrent writers.

The single ``tools._db`` aiosqlite connection is shared by every coroutine, and
a write span runs ``execute(INSERT)`` → ``await qdrant`` → ``commit()`` across
awaits. Without ``tools.get_write_lock()`` two writers interleave on that one
connection and the second caller's ``commit()`` flushes the first caller's
still-pending row — tearing the two-store write apart. These tests pin the lock
contract: protected spans run to completion one at a time, never interleaved.
"""

from __future__ import annotations

import asyncio

import pytest

from estormi_server.storage import tools

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fresh_write_lock():
    """Swap in a fresh lock per test.

    ``tools._write_lock`` is a module-level ``asyncio.Lock`` created at import
    time; pytest-asyncio runs each test on its own event loop, and an
    ``asyncio.Lock`` binds to the first loop it is awaited on — so reusing the
    import-time lock raises "bound to a different event loop". A fresh lock per
    test sidesteps that without touching production behaviour.
    """
    saved = tools._write_lock
    tools._write_lock = asyncio.Lock()
    try:
        yield
    finally:
        tools._write_lock = saved


async def _writer(label: str, trace: list[str], yield_inside: bool) -> None:
    """A leaf write span: take the lock, log enter→commit→exit.

    ``yield_inside`` forces an ``await asyncio.sleep(0)`` between the "enter" and
    "commit" markers so the event loop *could* switch to the other writer — the
    lock is what stops it from doing so mid-span.
    """
    async with tools.get_write_lock():
        trace.append(f"{label}:enter")
        if yield_inside:
            await asyncio.sleep(0)
        trace.append(f"{label}:commit")
        trace.append(f"{label}:exit")


def _is_serialised(trace: list[str]) -> bool:
    """True iff no writer's enter→exit span interleaves with another's."""
    depth_owner: str | None = None
    for event in trace:
        label, phase = event.split(":")
        if phase == "enter":
            if depth_owner is not None and depth_owner != label:
                return False
            depth_owner = label
        elif phase == "exit":
            if depth_owner != label:
                return False
            depth_owner = None
    return True


class TestWriteLockSerialisation:
    pytestmark = pytest.mark.integration

    async def test_concurrent_writers_do_not_interleave(self):
        # Two writers, each yielding inside their critical section. Without the
        # lock the sleep(0) would let the loop interleave the two spans; with it
        # the spans run strictly one-after-another.
        trace: list[str] = []
        await asyncio.gather(
            _writer("A", trace, yield_inside=True),
            _writer("B", trace, yield_inside=True),
        )

        assert _is_serialised(trace), f"writers interleaved under the lock: {trace}"
        # Both spans completed exactly once.
        assert trace.count("A:commit") == 1
        assert trace.count("B:commit") == 1

    async def test_unlocked_spans_would_interleave(self):
        # Control: the SAME two spans WITHOUT the lock DO interleave once a
        # writer yields mid-span — proving the test above actually exercises the
        # lock rather than passing vacuously.
        trace: list[str] = []

        async def _unlocked(label: str) -> None:
            trace.append(f"{label}:enter")
            await asyncio.sleep(0)
            trace.append(f"{label}:commit")
            trace.append(f"{label}:exit")

        await asyncio.gather(_unlocked("A"), _unlocked("B"))
        assert not _is_serialised(trace), f"expected interleave without lock: {trace}"

    async def test_lock_is_released_on_exception(self):
        # A writer that raises inside the span must still release the lock, or
        # every later writer deadlocks on the one shared connection.
        with pytest.raises(RuntimeError):
            async with tools.get_write_lock():
                raise RuntimeError("boom")

        # Lock is free again — a fresh acquisition returns immediately.
        await asyncio.wait_for(_writer("C", [], yield_inside=False), timeout=1.0)
