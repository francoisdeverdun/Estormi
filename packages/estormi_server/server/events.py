"""In-process event bus broadcasting engine lifecycle to UI subscribers.

Estormi has three engines (ingestion, briefing, and the optional distill) and
they never run in parallel. They start from either a manual REST call or a scheduled cron
tick. Before this bus the SPA's `LiveIndicator` only knew about *manual*
starts — its `sys.job` state was set from the page that fired the run, so
scheduled starts stayed invisible until the user changed page.

The bus exposes ``emit_started(kind, started_at)`` and
``emit_stopped(kind, status)``, plus a ``subscribe()`` async generator that
yields events for SSE clients. A new subscriber receives a one-shot
``engine.snapshot`` event first so the UI reconciles even when it connects
mid-run, then a live stream of ``engine.started`` / ``engine.stopped``.

Wire format (JSON in the SSE ``data`` field):
    {"type": "engine.snapshot",
     "current": {"kind": "ingestion", "startedAt": 1717070000.42} | null,
     "last":    {"kind": "briefing", "status": "ok", "endedAt": 1717069900.1} | null,
     "queue":   [{"kind": "briefing", "source": "schedule", "enqueuedAt": 1717070090.1}, ...]}
    {"type": "engine.started", "kind": "ingestion", "startedAt": 1717070000.42}
    {"type": "engine.stopped", "kind": "ingestion", "status": "ok", "endedAt": 1717070120.4}
    {"type": "queue.changed", "queue": [{"kind": "briefing", "source": "manual", "enqueuedAt": ...}]}

The queue half is driven by ``server.jobs`` (the single in-process FIFO that
all engine launches now flow through); this module owns the broadcast plumbing
and the engine-idle ``asyncio.Event`` that the queue runner waits on between
launches.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Literal

import structlog

log = structlog.get_logger()

EngineKind = Literal["ingestion", "briefing", "distill"]
EngineStatus = Literal["ok", "failed", "cancelled"]

# Current engine state — the in-process source of truth used to seed new
# subscribers. ``None`` means "no engine running".
_current_kind: EngineKind | None = None
_current_started_at: float | None = None

# Last finished engine — surfaced in the snapshot so a fresh client knows what
# just ran (the LiveIndicator's "last · …" line).
_last_kind: EngineKind | None = None
_last_status: EngineStatus | None = None
_last_ended_at: float | None = None

# Latest queue snapshot, mirrored from ``server.jobs`` after every mutation so
# new SSE subscribers can render the queue from the snapshot alone (no extra
# REST round-trip on connect).
_queue_snapshot: list[dict] = []

# Set when no engine is running. The queue runner waits on this between
# launches so it never tries to start a new engine before the previous one
# has actually finished. Initialised set (server starts idle).
_engine_idle_event = asyncio.Event()
_engine_idle_event.set()

# One bounded queue per open SSE client. We fan out via ``put_nowait`` so a
# slow client can't block producers; a full queue silently drops the event on
# the assumption that the next snapshot/start/stop catches the client up.
_subscribers: set[asyncio.Queue[dict]] = set()


def engine_idle_event() -> asyncio.Event:
    """asyncio.Event that's set whenever no engine is running."""
    return _engine_idle_event


def current_kind() -> EngineKind | None:
    """Engine currently tracked as running, or ``None``."""
    return _current_kind


def force_clear_current() -> None:
    """Reset the tracked-running kind without emitting a synthetic stopped event.

    Reconciliation hook for ``server.jobs.enqueue``: if a previous
    ``emit_stopped`` was swallowed because of a kind mismatch (preemption
    swap, dropped event), the bus can be left tracking an engine that no
    longer has a subprocess. The queue runner then deadlocks every future
    enqueue with ``already_running``. Callers detect that divergence (no
    matching ``_running`` marker, no live PID) and call this to unstick the
    state. Idempotent — no-op when nothing is tracked.
    """
    global _current_kind, _current_started_at
    if _current_kind is None:
        return
    log.warning("events.force_clear_current", stale_kind=_current_kind)
    _current_kind = None
    _current_started_at = None
    _engine_idle_event.set()


def publish_queue_changed(snapshot: list[dict]) -> None:
    """Mirror the latest queue snapshot and broadcast it to SSE subscribers.

    ``snapshot`` is the list of entries (already serialised dicts) in
    queue order. We store a shallow copy so later mutations in the caller
    don't retroactively change what subscribers see.
    """
    global _queue_snapshot
    _queue_snapshot = list(snapshot)
    _publish({"type": "queue.changed", "queue": _queue_snapshot})


def _snapshot_payload() -> dict:
    current = (
        None if _current_kind is None else {"kind": _current_kind, "startedAt": _current_started_at}
    )
    last = (
        None
        if _last_kind is None
        else {"kind": _last_kind, "status": _last_status, "endedAt": _last_ended_at}
    )
    return {
        "type": "engine.snapshot",
        "current": current,
        "last": last,
        "queue": list(_queue_snapshot),
    }


_DISCONNECT_SENTINEL: dict = {"type": "__disconnect__"}


def _publish(event: dict) -> None:
    """Fan an event out to every subscriber, evicting any whose queue is full.

    Previously a slow client (background tab, paused JS debugger) silently
    lost events: ``put_nowait`` raised ``QueueFull`` and we just logged.
    On reconnect the server's snapshot re-syncs current/last state, but the
    *intermediate* transitions in between were gone — which mattered when a
    cron-launched briefing started + finished while the page was hidden.

    Now we treat a full queue as a dead subscriber: send it a disconnect
    sentinel (best-effort), drop it from the set, and let the EventSource
    reconnect — the snapshot it gets on reconnect is authoritative.
    """
    dead: list[asyncio.Queue[dict]] = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
            log.warning("events.subscriber_evicted_full", event_type=event.get("type"))
    for q in dead:
        _subscribers.discard(q)
        try:
            q.put_nowait(_DISCONNECT_SENTINEL)
        except asyncio.QueueFull:
            # Already wedged — `subscribe`'s `finally` block will GC it once
            # the client closes.
            pass


def emit_started(kind: EngineKind, started_at: float | None = None) -> None:
    """Broadcast that ``kind`` has begun running.

    Idempotent — a second call for the same kind is a no-op, guarding against a
    double-fire. If a *different* engine was tracked as running, we emit a
    synthetic ``stopped`` for it first: the engine mutex preempts the old worker
    via SIGTERM and the real teardown's ``emit_stopped`` may not have landed yet.
    """
    global _current_kind, _current_started_at, _last_kind, _last_status, _last_ended_at
    if _current_kind == kind:
        return
    if _current_kind is not None:
        _last_kind = _current_kind
        _last_status = "cancelled"
        _last_ended_at = time.time()
        _publish(
            {
                "type": "engine.stopped",
                "kind": _current_kind,
                "status": "cancelled",
                "endedAt": _last_ended_at,
            }
        )
    ts = started_at if started_at is not None else time.time()
    _current_kind = kind
    _current_started_at = ts
    _engine_idle_event.clear()
    _publish({"type": "engine.started", "kind": kind, "startedAt": ts})


def emit_stopped(kind: EngineKind, status: EngineStatus = "ok") -> None:
    """Broadcast that ``kind`` has finished. No-op if it isn't the tracked one
    (preemption already cleared it, or a stop arrived twice)."""
    global _current_kind, _current_started_at, _last_kind, _last_status, _last_ended_at
    if _current_kind != kind:
        return
    _current_kind = None
    _current_started_at = None
    _last_kind = kind
    _last_status = status
    _last_ended_at = time.time()
    _engine_idle_event.set()
    _publish({"type": "engine.stopped", "kind": kind, "status": status, "endedAt": _last_ended_at})


async def subscribe() -> AsyncIterator[dict]:
    """Async generator for one SSE client: snapshot first, then live events."""
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
    _subscribers.add(queue)
    try:
        yield _snapshot_payload()
        while True:
            event = await queue.get()
            # Eviction sentinel from `_publish` — this subscriber was too
            # slow and the publisher dropped us. End the generator so the
            # SSE response closes; the browser EventSource will reconnect
            # and the next snapshot re-syncs state.
            if event is _DISCONNECT_SENTINEL:
                return
            yield event
    finally:
        _subscribers.discard(queue)
