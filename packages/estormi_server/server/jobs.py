"""Background job state and helpers for Estormi.

Owns the long-lived process handles, the APScheduler instance, the engine
queue + dispatch, and the cross-engine helpers (vault snapshots, engine-run
recording, log trimming, lock-recorded PGID kills). Per-engine spawn / wait /
log-handling logic lives in ``server.launchers.<engine>``; the launcher
functions are re-exported from this module so existing imports
(``from server.jobs import _launch_briefing`` etc.) keep working.

Every module-level global that used to live in ``main.py`` for ingestion
pipeline and Briefing scheduling lives here now — tests that used to
``patch("estormi_server.main._dag_lock")`` should now patch ``server.jobs._dag_lock``.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime  # noqa: F401 — see scheduler-trigger re-exports below
from pathlib import Path
from typing import Literal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger  # noqa: F401 — see re-exports below

from estormi_server.server import events as engine_events
from estormi_server.server.events import EngineKind  # re-exported: used for queue/engine type hints
from estormi_server.storage.tools import DATA_DIR
from memory_core import engine_lock

log = structlog.get_logger()

# ─── Project root resolution ─────────────────────────────────────────────────
# `packages/estormi_server/server/jobs.py` → ../../../.. resolves to the repo root in a source
# checkout and to the app-bundle resource root in a packaged deploy.
# Resolution order: ESTORMI_REPO_ROOT env var → that derived directory.
# Setting the env var is the supported way to point Estormi at a checkout that
# lives somewhere other than alongside this file.
_derived_root = Path(__file__).resolve().parent.parent.parent.parent
_env_repo_root = os.getenv("ESTORMI_REPO_ROOT", "").strip()
ROOT = Path(_env_repo_root) if _env_repo_root else _derived_root


def _resolve_self_url() -> str:
    """Resolve the URL of the running server for child subprocesses.

    Honours an explicit ``MCP_SERVER_URL`` if set, otherwise derives the
    loopback URL from ``MCP_SERVER_PORT`` (default 8000). The Tauri sidecar
    binds a non-8000 port when 8000 is taken, so hardcoding 8000 would point
    every spawned engine/ingest child at the wrong (or absent) server. Single
    source of truth so the briefing launcher and the ingestion pipeline
    launcher can't drift.
    """
    return os.getenv("MCP_SERVER_URL") or f"http://127.0.0.1:{os.getenv('MCP_SERVER_PORT', '8000')}"


def engine_subprocess_env(**extra: str) -> dict[str, str]:
    """Environment for an engine launched as ``python -m estormi_<engine>.run_*``.

    The briefing and distill launchers spawn with ``cwd=ROOT`` (so the child can
    reach ``scripts/`` etc.), but since the ``packages/`` move the first-party
    packages live under ``ROOT/packages`` — and the bundled Python only has
    ``memory_core`` pip-installed. Without ``ROOT/packages`` on ``PYTHONPATH`` the
    child dies with ``ModuleNotFoundError: No module named 'estormi_briefing' /
    'estormi_distill'`` in a packaged build. Mirror what ``scripts/daily_ingestion.sh``
    already does for the ingestion stages so every engine resolves identically.
    """
    pkgs = str(ROOT / "packages")
    existing = os.environ.get("PYTHONPATH", "")
    return {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "MCP_SERVER_URL": _resolve_self_url(),
        "PYTHONPATH": pkgs + (os.pathsep + existing if existing else ""),
        **extra,
    }


# ─── Scheduler + subprocess handles ──────────────────────────────────────────
_scheduler = AsyncIOScheduler()
_dag_lock = asyncio.Lock()
_dag_proc: asyncio.subprocess.Process | None = None
_dag_pgid: int | None = None  # captured at launch so stop works even after bash exits
_briefing_proc: asyncio.subprocess.Process | None = None
_distill_proc: asyncio.subprocess.Process | None = None

# Strong refs for fire-and-forget tasks. `asyncio.create_task` only holds a
# weak reference, so without this set the GC can collect a still-running
# task and silently cancel it (e.g. the engine log-close coroutines).
_background_tasks: set[asyncio.Task] = set()


def _track_background_task(task: asyncio.Task) -> asyncio.Task:
    """Register a fire-and-forget task so the GC can't drop it mid-flight.

    Returns the task so callers can keep their one-liner style. The done
    callback removes it from the set so the registry doesn't grow without
    bound.
    """
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


_DAG_MAIN_LOG = Path(DATA_DIR) / "logs" / "estormi-daily-dag.log"
_DAG_ERR_LOG = Path(DATA_DIR) / "logs" / "estormi-daily-dag-error.log"
_KNOWLEDGE_LOG = Path(DATA_DIR) / "logs" / "knowledge.log"


def _engine_status_from_returncode(rc: int | None) -> Literal["ok", "failed", "cancelled"]:
    """Translate a subprocess returncode into the engine-status taxonomy.

    Negative returncodes on Unix encode "killed by signal N" — that's how
    ``stop_other_engines`` preempts a running engine, so labelling those as
    ``"failed"`` makes the dashboard read like the engine crashed when it was
    actually mid-run when a higher-priority engine was dispatched. ``None``
    means the process never finished (we lost the handle); treat that the
    same as ``failed`` since we can't prove otherwise.
    """
    if rc == 0:
        return "ok"
    if rc is not None and rc < 0:
        return "cancelled"
    return "failed"


# ─── Engine-lock helpers ─────────────────────────────────────────────────────
# The DB-backed advisory lock (memory_core.engine_lock) is the single
# cross-process record of the live engine — it replaced the /tmp pid file AND
# the pattern-based pgrep/pkill probes (which could collaterally kill an
# unrelated process matching the argv). These thin async wrappers off-load the
# lock's sync sqlite access to a thread so the event loop never blocks on it.


async def _locked_pgid(kind: EngineKind) -> int | None:
    """PGID the lock records for ``kind`` (whether alive or not), else ``None``.

    Used by the kill paths as the authoritative target — survives a server
    restart because it lives in the DB, not process memory.
    """
    row = await asyncio.to_thread(engine_lock.current)
    if row is None or row.kind != kind:
        return None
    return row.pgid if row.pgid is not None else row.pid


async def _locked_alive(kind: EngineKind) -> bool:
    """True if the lock names a *live* owner of ``kind`` (signal-0 probed)."""
    row = await asyncio.to_thread(engine_lock.current)
    if row is None or row.kind != kind:
        return False
    return await asyncio.to_thread(engine_lock.is_alive, row)


async def _release_lock(kind: EngineKind, pid: int) -> None:
    """Best-effort release of the lock held by ``(kind, pid)`` — never raises."""
    try:
        await asyncio.to_thread(engine_lock.release, kind, pid)
    except Exception:
        log.exception("engine_lock.release_failed", kind=kind, pid=pid)


# ─── Engine mutex ────────────────────────────────────────────────────────────
# Estormi has three engine kinds: the ingestion pipeline, briefing generation,
# and the optional distillation chain. They share heavy resources (the local
# LLM, the Qdrant client, the sqlite DB) so running more than one at once is bad
# — slow at best, contradictory writes at worst. The product contract is: only
# one engine runs at a time. Manual and scheduled launches preempt (stop the
# others first).

ENGINES = ("ingestion", "briefing", "distill")

# `EngineKind` is re-exported from `server.events` (imported above) so SSE
# wire types and queue dispatch share one source of truth.

QueueSource = Literal["manual", "schedule", "backlog"]


# ─── Single canonical run queue ──────────────────────────────────────────────
# `_queue` + `_queue_runner` are the single entry point for all engine starts.
# Manual buttons, scheduled cron triggers, and backlog watchers all funnel
# through `enqueue(kind, source)`; the runner drains in FIFO order and waits
# for each engine to actually finish (via `engine_events.engine_idle_event()`)
# before launching the next. Dedupe is by kind: re-enqueueing while the same
# kind is already running or already waiting is a no-op. This kills the old
# "fake queue" the UI used to overlay from `/status` polling.


@dataclass(frozen=True)
class QueueEntry:
    kind: EngineKind
    source: QueueSource
    enqueued_at: float
    # Engine-specific knobs. Only ingestion currently carries one
    # (``{"stage_override": "<stage>"}``) for the per-source ▶ play button.
    # Kept as an opaque dict so adding a new knob doesn't require touching
    # QueueEntry's shape.
    payload: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "kind": self.kind,
            "source": self.source,
            "enqueuedAt": self.enqueued_at,
        }
        if self.payload:
            out["payload"] = dict(self.payload)
        return out


_queue: list[QueueEntry] = []
_queue_lock = asyncio.Lock()
_queue_changed = asyncio.Event()
# Set to the kind the runner is about to launch (or has launched and is
# awaiting completion of). Distinct from `engine_events.current_kind()` —
# the events bus reflects emit_started/stopped, which fire from inside the
# launchers. `_running` covers the gap between "popped from queue" and
# "subprocess actually spawned".
_running: EngineKind | None = None

EnqueueResult = Literal["queued", "already_queued", "already_running"]


def queue_snapshot() -> list[dict]:
    """Current queue, serialised. Safe for SSE / REST payloads."""
    return [e.to_dict() for e in _queue]


async def enqueue(
    kind: EngineKind,
    source: QueueSource = "manual",
    payload: dict | None = None,
) -> EnqueueResult:
    """Add ``kind`` to the queue, deduped by kind.

    Returns ``"queued"`` on success, ``"already_queued"`` if the same kind is
    already waiting, ``"already_running"`` if it's currently executing.
    Idempotent dedupe is the whole point — backlog watchers can fire every
    minute without piling up entries.
    """
    async with _queue_lock:
        if _running == kind:
            return "already_running"
        # Invariant (normal flow): the bus only tracks a kind that the runner
        # owns, so `current_kind()` non-None implies `_running` non-None — see
        # `_pop_next`, which sets `_running` *before* the launcher's
        # `emit_started`, and the runner, which clears `_running` only after the
        # idle event (re)fires on `emit_stopped`/`force_clear_current`. The only
        # way to observe `current_kind() != None` while `_running is None` is a
        # stale bus: a previous `emit_stopped` got swallowed (it no-ops on a
        # kind mismatch, e.g. a preemption swap), leaving the bus tracking an
        # engine no subprocess owns — and leaving the engine-idle event cleared.
        #
        # This reconcile covers the *same-kind* re-enqueue (the common UI retry):
        # clear the bus so the enqueue isn't rejected with a phantom
        # "already_running". A *different*-kind enqueue against a stale bus
        # enqueues fine and no longer wedges the runner: `_await_engine_idle`
        # bounds the idle-wait and self-heals a stale event whose engine is
        # confirmed dead, so the swallowed-`emit_stopped` deadlock can't persist.
        # The queue runner is the source of truth for "is something running";
        # trust `_running`.
        if engine_events.current_kind() == kind:
            if _running is None:
                log.warning("queue.bus_state_stale_clearing", stale_kind=kind)
                engine_events.force_clear_current()
            else:
                return "already_running"
        if any(e.kind == kind for e in _queue):
            return "already_queued"
        _queue.append(
            QueueEntry(
                kind=kind,
                source=source,
                enqueued_at=time.time(),
                payload=payload,
            )
        )
        snapshot = [e.to_dict() for e in _queue]
    _queue_changed.set()
    engine_events.publish_queue_changed(snapshot)
    log.info("queue.enqueued", kind=kind, source=source, depth=len(snapshot))
    return "queued"


async def clear_queue() -> int:
    """Drop every waiting entry. Does not touch the currently running engine.

    Returns the number of entries cleared.
    """
    async with _queue_lock:
        n = len(_queue)
        _queue.clear()
        snapshot: list[dict] = []
    if n:
        engine_events.publish_queue_changed(snapshot)
        log.info("queue.cleared", dropped=n)
    return n


async def remove_from_queue(kind: EngineKind) -> bool:
    """Drop the single waiting entry for ``kind``. Returns True if removed.

    The currently running engine is never touched — Clear/Remove drains the
    waiting list, Stop is a separate concern. Dedupe by kind means there's
    at most one entry per kind to look for.
    """
    async with _queue_lock:
        before = len(_queue)
        _queue[:] = [e for e in _queue if e.kind != kind]
        removed = len(_queue) < before
        snapshot = [e.to_dict() for e in _queue]
    if removed:
        engine_events.publish_queue_changed(snapshot)
        log.info("queue.removed", kind=kind, depth=len(snapshot))
    return removed


async def _pop_next() -> QueueEntry | None:
    """Pop the head entry and mark `_running`. Returns ``None`` when empty."""
    global _running
    async with _queue_lock:
        if not _queue:
            return None
        # Invariant check (see `enqueue`): the runner only pops once the prior
        # engine is idle, so the bus must show no engine here. A non-None
        # `current_kind()` at pop time means a swallowed `emit_stopped` left the
        # bus stale — the divergence that can wedge the idle-wait. Log it so the
        # glitch is observable instead of silent; the pop proceeds regardless
        # (the runner has already cleared `_running` and awaited idle).
        stale = engine_events.current_kind()
        if stale is not None:
            log.warning("queue.pop_with_stale_bus", stale_kind=stale, popping=_queue[0].kind)
        entry = _queue.pop(0)
        _running = entry.kind
        snapshot = [e.to_dict() for e in _queue]
    engine_events.publish_queue_changed(snapshot)
    return entry


# How long the runner blocks on the idle event before re-validating ground
# truth. Engines run for minutes, so a 30 s recheck is cheap (a signal-0 probe
# via the engine lock) yet bounds how long a wedged runner can stay stuck.
_IDLE_RECHECK_SECS = 30.0


async def _engine_process_alive(kind: EngineKind) -> bool:
    """Best-effort ground truth: does ``kind`` actually have a live process?

    Used only by ``_await_engine_idle`` to tell a genuinely long-running engine
    apart from a stale idle event (a lost ``emit_stopped``). Read-only — never
    kills anything — and errs toward ``True`` on any uncertainty so a real,
    still-running engine is never healed away; only a *confirmed-dead* one
    unsticks the runner.
    """
    try:
        if kind == "briefing":
            return await _briefing_running()
        if kind == "ingestion":
            if _dag_proc is not None and _dag_proc.returncode is None:
                return True
            # No in-process handle (server restarted, or shell-launched run) —
            # consult the cross-process lock, which is signal-0 probed and errs
            # toward alive on uncertainty, so a real run is never healed away.
            return await _locked_alive("ingestion")
        if kind == "distill":
            return await _distill_running()
    except Exception:
        return True  # uncertain → assume alive, never heal a maybe-live engine
    return True


async def _await_engine_idle() -> None:
    """Wait until the engine slot is idle, self-healing a stale idle event.

    The runner gates each launch on ``engine_idle_event()``, set by
    ``emit_stopped`` / ``force_clear_current``. A lost stop — a preemption swap
    whose ``emit_stopped`` no-oped on a kind mismatch, or an admin
    ``force_clear_current`` racing a still-live engine — can leave the event
    cleared with no process to ever re-set it, which used to wedge the runner
    forever (the different-kind enqueue residual documented in ``enqueue``).

    So we don't trust the event blindly: every ``_IDLE_RECHECK_SECS`` we check
    ground truth. If the engine the slot is blocking on is confirmed dead, a
    stop was lost — heal (force-clear the bus, which re-sets the event) and
    return. The probe is read-only and assumes "alive" on uncertainty, so a
    genuinely long-running engine keeps the runner waiting exactly as before;
    only a truly dead one unsticks it.
    """
    ev = engine_events.engine_idle_event()
    while not ev.is_set():
        try:
            await asyncio.wait_for(ev.wait(), timeout=_IDLE_RECHECK_SECS)
            return
        except asyncio.TimeoutError:
            owner = _running or engine_events.current_kind()
            if owner is not None and await _engine_process_alive(owner):
                continue  # genuinely running — keep waiting
            log.warning(
                "queue.idle_wait_healed",
                owner=owner,
                running=_running,
                bus=engine_events.current_kind(),
            )
            engine_events.force_clear_current()  # re-sets the idle event
            ev.set()  # belt-and-suspenders for the owner-None stale-event case
            return


async def _queue_runner() -> None:
    """Drain the queue forever, one engine at a time.

    Loop shape: wait for a wakeup, then drain everything we can — wait for
    the engine slot to be idle, pop an entry, launch its engine. On the next
    iteration the idle-wait blocks until ``emit_stopped`` fires for that
    engine, so the runner never has two engines in flight at once.
    Cancellation (server shutdown) exits cleanly.

    ``_running`` is set by ``_pop_next`` and kept set across the launcher
    call + the subsequent wait for completion — it spans the whole "this
    engine is the runner's responsibility" window so concurrent ``enqueue``
    calls see the kind as in-flight and refuse to duplicate.

    The body is wrapped so any unexpected exception (anywhere outside the
    inner dispatch try/except) logs and self-restarts the loop instead of
    killing the task. A dead runner leaves enqueued items stranded forever,
    so resilience here is load-bearing.
    """
    global _running
    while True:
        try:
            await _queue_changed.wait()
            _queue_changed.clear()
            while True:
                # Wait for the previous engine to fully finish before considering
                # another launch. On the very first iteration this is already set
                # (server starts idle); from then on, it blocks until emit_stopped
                # — but self-heals if that stop was lost (see `_await_engine_idle`).
                await _await_engine_idle()
                # The prior launch (if any) is now complete — clear the stale
                # `_running` marker before we look at the next entry, otherwise
                # enqueue dedupe will reject a re-run of the same kind.
                async with _queue_lock:
                    _running = None
                entry = await _pop_next()
                if entry is None:
                    break
                try:
                    await _dispatch(entry)
                except Exception:
                    # Don't propagate — drain the next entry instead of locking up.
                    log.exception("queue_runner.launch_failed", kind=entry.kind)
                    continue
                # The launcher kicked off the engine and emit_started cleared the
                # idle event. The next iteration's idle-wait blocks on emit_stopped.
        except asyncio.CancelledError:
            raise
        except Exception:
            # Anything else (a bug in the wait/lock/snapshot plumbing) must
            # not strand the queue. Log, prime the event so we don't sleep
            # forever on a stale clear, and loop.
            log.exception("queue_runner.crashed_restarting")
            _queue_changed.set()
            await asyncio.sleep(0.5)


async def _dispatch(entry: QueueEntry) -> None:
    """Route a QueueEntry to its launcher, passing through per-engine knobs.

    Ingestion reads ``stage_override``; briefing reads ``refresh`` (health) and
    ``notify`` (force / silent) off ``entry.payload``.
    """
    if entry.kind == "ingestion":
        stage = (entry.payload or {}).get("stage_override")
        await _run_dag(stage_override=stage)
    elif entry.kind == "briefing":
        await _launch_briefing(
            entry.source,
            refresh=(entry.payload or {}).get("refresh"),
            notify=(entry.payload or {}).get("notify"),
        )
    elif entry.kind == "distill":
        await _launch_distill(entry.source)
    else:
        raise ValueError(f"unknown engine kind: {entry.kind!r}")


async def stop_engine(kind: EngineKind) -> None:
    """Kill the processes for ``kind`` so the queue can advance.

    Best-effort: each launcher's ``_close_log_on_exit`` task waits on the
    subprocess and fires ``emit_stopped`` once it exits, which is what
    unblocks the queue runner to dispatch the next entry. We don't emit
    here — emitting twice would race the launcher's own status reporting.
    """
    if kind == "ingestion":
        await _kill_dag_processes()
    elif kind == "briefing":
        await _kill_briefing()
    elif kind == "distill":
        await _kill_distill()


async def stop_other_engines(current: str) -> None:
    """Stop every engine except ``current`` before a fresh start.

    Idempotent and best-effort — each kill is wrapped in its own try/except
    so a failure in one engine's shutdown can't block the others. After
    this returns the caller may safely launch its own engine.

    ``current`` is normally one of ``ENGINES`` (the engine about to start);
    the ``"reset"`` sentinel means "stop everything" — used by the admin
    reset endpoints to ensure no engine keeps writing into tables they are
    about to truncate.
    """
    if current != "reset" and current not in ENGINES:
        log.warning("engine_mutex.unknown_engine", current=current)
        return
    if current != "ingestion":
        try:
            await _kill_dag_processes()
        except Exception:
            log.exception("engine_mutex.kill_dag_failed")
    if current != "briefing":
        try:
            await _kill_briefing()
        except Exception:
            log.exception("engine_mutex.kill_briefing_failed")
    if current != "distill":
        try:
            await _kill_distill()
        except Exception:
            log.exception("engine_mutex.kill_distill_failed")
    log.info("engine_mutex.cleared_others", current=current)


def _start_engine_dag_run(engine: str, log_path: str, trigger: str = "manual") -> int | None:
    """Record a new ``dag_runs`` row for an engine launch.

    Returns the new ``run_id`` so ``_finish_engine_dag_run`` can close it,
    or ``None`` if the row could not be written — best-effort: history
    surfacing must not block engine launches.
    """
    try:
        from memory_core.dag_state import start_run  # noqa: PLC0415

        return start_run(
            trigger=trigger,
            log_path=log_path,
            err_path=log_path,
            engine=engine,
        )
    except Exception:
        log.exception("engine_run.start_failed", engine=engine)
        return None


def _finish_engine_dag_run(run_id: int | None, status: str) -> None:
    """Close a ``dag_runs`` row opened by ``_start_engine_dag_run``."""
    if run_id is None:
        return
    try:
        from memory_core.dag_state import finish_run  # noqa: PLC0415

        finish_run(run_id, status)
    except Exception:
        log.exception("engine_run.finish_failed", run_id=run_id, status=status)


def _trim_log_history(path: str, max_lines: int = 3000) -> None:
    """Cap a rolling engine log at its last ``max_lines`` lines.

    Engine logs are appended to across runs so a restart keeps prior history
    in view; trimming on each launch stops the file from growing without bound.
    """
    try:
        with open(path, "rb") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return
    if len(lines) > max_lines:
        with open(path, "wb") as fh:
            fh.writelines(lines[-max_lines:])


# Per-run log capture. Engine stdout/stderr are appended to rolling files
# shared across runs; we snapshot each file's byte length at launch and read
# from that offset to EOF at end-of-run to recover *this* run's slice, which is
# written to its own ``engine-logs/<run_id>.log`` file in the vault.
_RUN_LOG_MAX_BYTES = 200_000


def _log_size(path: str) -> int:
    """Byte length of ``path`` right now, or 0 if it doesn't exist yet."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_log_slice(path: str, start: int, max_bytes: int = _RUN_LOG_MAX_BYTES) -> str:
    """Return the text appended to ``path`` since byte offset ``start``.

    Reads from ``start`` to EOF — this run's slice of a rolling log — and caps
    the result to the last ``max_bytes``, dropping the leading partial line and
    prefixing a marker when it overflows. Returns "" on any read error.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, start))
            data = fh.read()
    except OSError:
        return ""
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1 :]
        data = b"... (truncated) ...\n" + data
    return data.decode("utf-8", errors="replace")


# ─── Vault snapshots ─────────────────────────────────────────────────────────
# Each engine appends a run record to the iCloud Drive vault history
# (``engines_history.json``) so the iOS companion can plot a time series.
# Best-effort — a failure never affects the engine. The briefing engine writes
# its own ``briefings/<date>.json`` from ``run_briefing.py``.


# The vault-metrics *reporting* concern — the engine-run history recorder, the
# companion metrics-snapshot builder, the chart time-series shaper, and the
# stale-watermark probe — lives in ``server.vault_metrics``. It is re-exported
# here so the launchers' ``_jobs._record_engine_run`` call and the test suite's
# ``jobs._build_vault_metrics`` / ``_build_timeseries`` references (and their
# patches) keep resolving through ``server.jobs``.
from estormi_server.server.vault_metrics import (  # noqa: E402
    _VAULT_METRICS_WINDOW_DAYS,  # noqa: F401
    _WATERMARK_STALE_DAYS,  # noqa: F401
    _build_timeseries,  # noqa: F401
    _build_vault_metrics,  # noqa: F401
    _record_engine_run,  # noqa: F401
    _stale_watermarks,  # noqa: F401
)


async def _schedule_ingestion() -> None:
    """APScheduler-facing wrapper for the daily ingestion pipeline cron."""
    await enqueue("ingestion", "schedule")


async def _schedule_briefing() -> None:
    """APScheduler-facing wrapper for the daily briefing cron."""
    await enqueue("briefing", "schedule")


async def _schedule_distill() -> None:
    """APScheduler-facing wrapper for the weekly quill-distillation cron."""
    await enqueue("distill", "schedule")


# ─── Launcher + scheduler-trigger re-exports ─────────────────────────────────
# Both import groups below live *after* the module-level state above is defined,
# because the sibling modules reach back into this one by attribute and the
# import cycle must resolve against a fully-initialised ``server.jobs``.
#
# Launchers: each engine's spawn / wait / log-handling logic lives in
# ``server.launchers.<engine>``; the shared mutable state (proc handles, log
# paths, lock) stays here so test patches on ``server.jobs.<name>`` keep driving
# those code paths. The re-exports preserve every import that used to read these
# symbols off ``server.jobs``.
#
# Schedulers: the system-wake catch-up and the WHOOP morning poller — the
# concerns that decide *when* to enqueue — live in ``server.schedulers``. They
# reach back into this module's shared state (``_scheduler``, ``enqueue``,
# ``datetime``, ``IntervalTrigger``, ``log``) by attribute via
# ``from estormi_server.server import jobs as _jobs``, so test patches on
# ``server.jobs.<name>`` keep driving them. Re-exported here so
# ``from server.jobs import wake_catchup`` / ``apply_whoop_polling_schedule`` and
# the test suite's ``jobs._most_recent_fire`` / ``jobs._schedule_whoop_poll``
# references keep resolving.
from estormi_server.server.launchers.briefing import (  # noqa: E402, F401
    _briefing_running,
    _kill_briefing,
    _launch_briefing,
)
from estormi_server.server.launchers.distill import (  # noqa: E402, F401
    _distill_running,
    _kill_distill,
    _launch_distill,
)
from estormi_server.server.launchers.ingestion import (  # noqa: E402, F401
    _DEFAULT_DEPTH,
    _DEPTH_DEFAULTS,
    _DEPTH_ENV,
    _DEPTH_TO_DAYS,
    _ROOT_REQUIRED_STAGES,
    _kill_dag_processes,
    _registry,
    _run_dag,
    _settings_snapshot,
    _stage_runnable,
    apply_ingest_env_overrides,
)
from estormi_server.server.schedulers import (  # noqa: E402, F401
    _CATCHUP_ENGINES,
    _WHOOP_POLL_JOB_ID,
    _most_recent_fire,
    _schedule_whoop_poll,
    apply_whoop_polling_schedule,
    wake_catchup,
)
