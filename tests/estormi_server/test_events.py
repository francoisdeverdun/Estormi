"""Behaviour tests for the in-process engine-event bus (``server/events.py``).

The bus is the single source of truth the SSE endpoint and the queue runner
both read: ``emit_started`` / ``emit_stopped`` mutate the tracked engine and
fan events out to subscriber queues, ``subscribe()`` yields a one-shot
``engine.snapshot`` then a live stream, and a too-slow subscriber is *evicted*
(its queue filled) rather than silently starved. These tests drive the real
coroutines and assert on the observable effects — the events a subscriber
receives, the tracked state after a transition, the idle ``Event`` the runner
gates on — never merely that a function was called.

The module keeps its whole state in module-level globals (``_current_kind``,
``_last_kind``, ``_subscribers``, the idle ``Event`` …). The autouse
``_reset_events_globals`` fixture below resets every one of them before and
after each test so neither these tests nor any other event test in the suite
leaks state into a neighbour — and rebinds the idle ``Event`` to the running
loop, since it is created at import and would otherwise raise
"bound to a different event loop" under pytest-asyncio's per-test loops.
"""

from __future__ import annotations

import asyncio

import pytest

from estormi_server.server import events

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_events_globals():
    """Reset every ``server.events`` module global around each test.

    The bus tracks the running/last engine, the queue mirror, the open
    subscriber set, and the engine-idle ``Event`` in module globals. A leaked
    subscriber would receive a later test's broadcast; a leaked ``_current_kind``
    would make ``emit_stopped`` no-op unexpectedly; and the idle ``Event``,
    created once at import, binds to the first loop that awaits it, so a later
    ``.wait()`` under a fresh per-test loop raises. Rebinding + clearing here
    keeps every event test isolated and loop-clean.
    """

    def _reset():
        events._current_kind = None
        events._current_started_at = None
        events._last_kind = None
        events._last_status = None
        events._last_ended_at = None
        events._queue_snapshot = []
        events._subscribers = set()
        events._engine_idle_event = asyncio.Event()
        events._engine_idle_event.set()

    _reset()
    yield
    _reset()


async def _drain(queue: asyncio.Queue) -> list[dict]:
    """Non-blocking pull of every event currently waiting in ``queue``."""
    out: list[dict] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# ── subscribe(): snapshot-first, then live stream ─────────────────────────────


class TestSubscribeSnapshot:
    """A fresh subscriber must receive a one-shot ``engine.snapshot`` reflecting
    current/last/queue state before any live event, so a client connecting
    mid-run reconciles without a REST round-trip."""

    async def test_first_yield_is_snapshot_with_idle_state(self):
        agen = events.subscribe()
        first = await agen.__anext__()
        assert first["type"] == "engine.snapshot"
        assert first["current"] is None
        assert first["last"] is None
        assert first["queue"] == []
        await agen.aclose()

    async def test_snapshot_reflects_running_engine(self):
        events.emit_started("ingestion", started_at=1717070000.0)
        agen = events.subscribe()
        snap = await agen.__anext__()
        assert snap["current"] == {"kind": "ingestion", "startedAt": 1717070000.0}
        await agen.aclose()

    async def test_snapshot_reflects_last_finished_engine(self):
        events.emit_started("briefing", started_at=10.0)
        events.emit_stopped("briefing", status="ok")
        agen = events.subscribe()
        snap = await agen.__anext__()
        assert snap["current"] is None
        assert snap["last"]["kind"] == "briefing"
        assert snap["last"]["status"] == "ok"
        await agen.aclose()

    async def test_subscriber_registered_then_unregistered_on_close(self):
        """Entering the generator adds the queue to ``_subscribers``; closing it
        removes it — no leak of a dead client's slot."""
        agen = events.subscribe()
        await agen.__anext__()  # registers
        assert len(events._subscribers) == 1
        await agen.aclose()  # finally-block discard
        assert len(events._subscribers) == 0

    async def test_live_event_after_snapshot_is_forwarded(self):
        """After the snapshot, a real ``emit_started`` reaches the subscriber."""
        agen = events.subscribe()
        await agen.__anext__()  # snapshot
        events.emit_started("distill", started_at=42.0)
        live = await asyncio.wait_for(agen.__anext__(), timeout=1)
        assert live == {"type": "engine.started", "kind": "distill", "startedAt": 42.0}
        await agen.aclose()


# ── _publish(): fan-out + full-queue eviction ─────────────────────────────────


class TestPublishFanout:
    async def test_publish_reaches_every_subscriber(self):
        q1: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        q2: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events._subscribers = {q1, q2}
        events._publish({"type": "queue.changed", "queue": []})
        assert (await _drain(q1)) == [{"type": "queue.changed", "queue": []}]
        assert (await _drain(q2)) == [{"type": "queue.changed", "queue": []}]

    async def test_full_subscriber_is_evicted_while_healthy_one_gets_the_event(self):
        """A subscriber whose queue is full is treated as dead: dropped from the
        set so its ``subscribe`` generator ends and the EventSource reconnects.
        A healthy peer still receives the broadcast — one slow client must not
        starve the others."""
        full: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
        full.put_nowait({"type": "engine.started", "kind": "ingestion"})  # now full
        healthy: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events._subscribers = {full, healthy}

        events._publish({"type": "engine.stopped", "kind": "ingestion", "status": "ok"})

        assert full not in events._subscribers  # evicted
        assert healthy in events._subscribers  # spared
        assert (await _drain(healthy)) == [
            {"type": "engine.stopped", "kind": "ingestion", "status": "ok"}
        ]

    async def test_evicted_subscriber_receives_disconnect_sentinel(self):
        """An evicted subscriber is handed the disconnect sentinel (best-effort)
        so its generator returns promptly rather than waiting for client-close GC.

        ``_publish`` rejects the *event* push (queue full) but the subsequent
        sentinel push succeeds when the queue has room. A queue that refuses the
        event yet accepts the sentinel models that window deterministically."""

        class _FullForEvents(asyncio.Queue):
            """Raises QueueFull on the real event, accepts the sentinel."""

            def put_nowait(self, item):
                if item is events._DISCONNECT_SENTINEL:
                    return super().put_nowait(item)
                raise asyncio.QueueFull

        q = _FullForEvents(maxsize=8)
        events._subscribers = {q}

        events._publish({"type": "engine.started", "kind": "briefing"})

        assert q not in events._subscribers  # evicted from the live set
        # The sentinel was delivered, so the subscriber loop can end cleanly.
        assert q.get_nowait() is events._DISCONNECT_SENTINEL

    async def test_subscribe_returns_on_disconnect_sentinel(self):
        """A subscriber generator that receives the disconnect sentinel ends
        cleanly (StopAsyncIteration), closing the SSE response."""
        agen = events.subscribe()
        await agen.__anext__()  # snapshot — registers the queue
        (queue,) = tuple(events._subscribers)
        queue.put_nowait(events._DISCONNECT_SENTINEL)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=1)
        await agen.aclose()


# ── _snapshot_payload(): pure shaping ─────────────────────────────────────────


class TestSnapshotPayload:
    def test_idle_payload_is_all_null(self):
        payload = events._snapshot_payload()
        assert payload == {
            "type": "engine.snapshot",
            "current": None,
            "last": None,
            "queue": [],
        }

    def test_payload_includes_current_last_and_queue(self):
        events._current_kind = "ingestion"
        events._current_started_at = 100.0
        events._last_kind = "briefing"
        events._last_status = "failed"
        events._last_ended_at = 90.0
        events._queue_snapshot = [{"kind": "distill", "source": "schedule"}]

        payload = events._snapshot_payload()
        assert payload["current"] == {"kind": "ingestion", "startedAt": 100.0}
        assert payload["last"] == {"kind": "briefing", "status": "failed", "endedAt": 90.0}
        assert payload["queue"] == [{"kind": "distill", "source": "schedule"}]

    def test_payload_queue_is_a_copy(self):
        """The payload's queue must be a snapshot copy — a later mutation of the
        module global must not retroactively change an already-built payload."""
        events._queue_snapshot = [{"kind": "ingestion"}]
        payload = events._snapshot_payload()
        events._queue_snapshot.append({"kind": "briefing"})
        assert payload["queue"] == [{"kind": "ingestion"}]


# ── emit_started / emit_stopped: state transitions + idle event ───────────────


class TestEmitStarted:
    async def test_started_sets_tracked_kind_and_clears_idle(self):
        assert events.engine_idle_event().is_set()  # baseline: idle
        events.emit_started("ingestion", started_at=5.0)
        assert events.current_kind() == "ingestion"
        assert not events.engine_idle_event().is_set()

    async def test_started_is_idempotent_for_same_kind(self):
        """A repeated start for the kind already running is a no-op — no second
        ``engine.started`` event, so manual paths that also call this don't
        double-fire."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events._subscribers = {q}
        events.emit_started("briefing", started_at=1.0)
        events.emit_started("briefing", started_at=2.0)  # duplicate
        starts = [e for e in await _drain(q) if e["type"] == "engine.started"]
        assert len(starts) == 1
        assert starts[0]["startedAt"] == 1.0  # the original wins

    async def test_started_preempts_other_kind_with_synthetic_stop(self):
        """Starting a *different* engine while one is tracked emits a synthetic
        ``engine.stopped`` (status ``cancelled``) for the old one first — the
        mutex SIGTERMs the old worker and its real ``emit_stopped`` may not have
        landed yet."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events._subscribers = {q}
        events.emit_started("ingestion", started_at=1.0)
        events.emit_started("briefing", started_at=2.0)  # preempts ingestion

        seen = await _drain(q)
        stopped = [e for e in seen if e["type"] == "engine.stopped"]
        started = [e for e in seen if e["type"] == "engine.started"]
        # A synthetic cancelled-stop for ingestion, then a start for briefing.
        assert stopped[-1]["kind"] == "ingestion"
        assert stopped[-1]["status"] == "cancelled"
        assert started[-1]["kind"] == "briefing"
        # Tracked state followed through to the new engine; last = the preempted.
        assert events.current_kind() == "briefing"
        assert events._last_kind == "ingestion"
        assert events._last_status == "cancelled"


class TestEmitStopped:
    async def test_stopped_clears_tracked_kind_and_sets_idle(self):
        events.emit_started("ingestion", started_at=1.0)
        events.emit_stopped("ingestion", status="ok")
        assert events.current_kind() is None
        assert events.engine_idle_event().is_set()
        assert events._last_kind == "ingestion"
        assert events._last_status == "ok"

    async def test_stopped_for_wrong_kind_is_a_noop(self):
        """A stop for a kind that isn't the tracked one (preemption already
        cleared it, or a duplicate stop) leaves state untouched and emits
        nothing — the headline kind-mismatch no-op."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events.emit_started("ingestion", started_at=1.0)
        events._subscribers = {q}  # attach only now, to catch the stop's events
        idle_before = events.engine_idle_event().is_set()

        events.emit_stopped("briefing", status="ok")  # not the tracked kind

        assert events.current_kind() == "ingestion"  # unchanged
        assert events._last_kind is None  # no last recorded
        assert events.engine_idle_event().is_set() == idle_before  # still cleared
        assert await _drain(q) == []  # no event published

    @pytest.mark.parametrize("status", ["ok", "failed", "cancelled"])
    async def test_stopped_records_the_reported_status(self, status):
        events.emit_started("distill", started_at=1.0)
        events.emit_stopped("distill", status=status)
        assert events._last_status == status


# ── force_clear_current: reconciliation hook ──────────────────────────────────


class TestForceClearCurrent:
    async def test_clears_tracked_kind_and_resets_idle_without_event(self):
        """The reconcile hook unsticks a stale bus: it clears the tracked kind
        and re-sets the idle event, but emits no ``stopped`` (no subscriber
        should see a phantom transition)."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events.emit_started("ingestion", started_at=1.0)
        events._subscribers = {q}
        await _drain(q)  # discard the started event

        events.force_clear_current()

        assert events.current_kind() is None
        assert events.engine_idle_event().is_set()
        assert await _drain(q) == []  # explicitly no synthetic event

    async def test_is_a_noop_when_nothing_tracked(self):
        assert events.current_kind() is None
        events.force_clear_current()  # must not raise
        assert events.current_kind() is None


# ── publish_queue_changed: mirror + broadcast ─────────────────────────────────


class TestPublishQueueChanged:
    async def test_mirrors_snapshot_and_broadcasts(self):
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        events._subscribers = {q}
        snapshot = [{"kind": "ingestion", "source": "manual"}]
        events.publish_queue_changed(snapshot)
        assert events._queue_snapshot == snapshot
        assert await _drain(q) == [{"type": "queue.changed", "queue": snapshot}]

    async def test_stored_mirror_is_decoupled_from_caller_list(self):
        """A shallow copy is stored so a later mutation of the caller's list
        doesn't retroactively change what new subscribers see in the snapshot."""
        caller_list = [{"kind": "briefing"}]
        events.publish_queue_changed(caller_list)
        caller_list.append({"kind": "distill"})
        assert events._queue_snapshot == [{"kind": "briefing"}]
