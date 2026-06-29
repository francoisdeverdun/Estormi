"""Briefing engine launcher.

State (the ``_briefing_proc`` handle, ``_KNOWLEDGE_LOG`` path) and helpers
(``_trim_log_history``, ``_start/_finish_engine_dag_run``,
``_record_engine_run``, ``stop_other_engines``) all live in
``server.jobs``; we reach back through that module so test patches on
``server.jobs.<name>`` still drive this code path.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime

import structlog

from estormi_server.server import jobs as _jobs
from estormi_server.server.events import emit_started, emit_stopped

log = structlog.get_logger()


async def _briefing_running() -> bool:
    """True while briefing generation (``run_briefing``) is in progress.

    Prefers the in-process handle; falls back to the engine lock (which survives
    a server restart and is signal-0 probed) instead of the old ``pgrep -f``
    pattern match.
    """
    if _jobs._briefing_proc and _jobs._briefing_proc.returncode is None:
        return True
    _jobs._briefing_proc = None
    return await _jobs._locked_alive("briefing")


async def _kill_briefing_proc(proc) -> None:
    """SIGTERM (then SIGKILL) a live briefing child's whole process group.

    The child is spawned with ``start_new_session=True`` (PGID == PID), so
    ``killpg`` reaches the ``run_briefing`` process *and* the ``claude`` CLI
    grandchildren it shells out to — a PID-only kill would orphan them. Falls
    back to a PID kill if the group is already gone. Does not release the lock;
    callers own that.
    """
    import signal as _signal  # noqa: PLC0415

    if proc is None or proc.returncode is not None:
        return
    try:
        _jobs.os.killpg(proc.pid, _signal.SIGTERM)
    except (ProcessLookupError, OSError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            _jobs.os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        await proc.wait()


async def _kill_briefing() -> None:
    """Stop a running briefing by its process group, then release the lock.

    Kills the whole group (child + ``claude`` CLI grandchildren) — never the
    old ``pkill -f`` pattern, which could collaterally match an unrelated
    process. On a server restart with no in-process handle, the lock's recorded
    PGID is the kill target instead.
    """
    import signal as _signal  # noqa: PLC0415

    proc = _jobs._briefing_proc
    pid: int | None = None
    if proc and proc.returncode is None:
        pid = proc.pid
        await _kill_briefing_proc(proc)
    else:
        # No in-process handle (server restarted): use the lock's recorded pgid
        # so the kill still reaches the whole tree.
        pid = await _jobs._locked_pgid("briefing")
        if pid is not None:
            try:
                _jobs.os.killpg(pid, _signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    _jobs._briefing_proc = None
    if pid is not None:
        await _jobs._release_lock("briefing", pid)


async def _launch_briefing(
    trigger: str = "manual", refresh: str | None = None, notify: str | None = None
) -> None:
    """Preempt the other engines and launch a briefing-generation run.

    Briefing generation (fetch -> LLM -> build -> deliver) is LLM-heavy, so it
    takes the single engine slot. Cron- and manually-launched alike. The
    composed digest is delivered to the vault only — it is never re-ingested as
    chunk memory, since its raw material is already in the DB and searchable.

    ``refresh="health"`` runs the wake-time readiness refresh instead of the
    full pipeline (same subprocess entrypoint, same mutex/history plumbing —
    see ``estormi_briefing.refresh_health``). ``notify="force"`` makes the run
    announce its briefing to the iOS companion and ``"silent"`` suppresses it;
    omitted, the run decides from settings (the WHOOP wake-trigger owns the
    silent morning pre-compute — see ``run_briefing._decide_notify``).
    """
    await _jobs.stop_other_engines("briefing")
    env = _jobs.engine_subprocess_env()
    if refresh:
        env["ESTORMI_BRIEFING_REFRESH"] = refresh
    if notify:
        env["ESTORMI_BRIEFING_NOTIFY"] = notify
    _jobs._KNOWLEDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    _jobs._trim_log_history(str(_jobs._KNOWLEDGE_LOG))
    # Snapshot the log length before the run header so the end-of-run record
    # captures this run's slice (header + child output) of the shared file.
    log_start = _jobs._log_size(str(_jobs._KNOWLEDGE_LOG))
    run_header = f"\n── run started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──\n".encode()
    # Append so prior runs stay visible across restarts — matches the ingestion
    # launcher; ``_trim_log_history`` caps the file size.
    out = open(_jobs._KNOWLEDGE_LOG, "ab", buffering=0)  # noqa: SIM115 — append; history kept across runs
    run_id = await asyncio.to_thread(
        _jobs._start_engine_dag_run, "briefing", str(_jobs._KNOWLEDGE_LOG), trigger
    )
    _log = log.bind(run_id=run_id)
    started_at = time.time()
    try:
        out.write(run_header)
        # ``start_new_session=True`` puts the child in its own process group so
        # ``_kill_briefing`` can ``killpg`` the whole tree — the run_briefing
        # child shells out to the ``claude`` CLI, and a PID-only kill would
        # orphan those grandchildren. With a fresh session PGID == PID.
        _jobs._briefing_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "estormi_briefing.run_briefing",
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(_jobs.ROOT),
            start_new_session=True,
        )
        # Take the cross-process engine slot. stop_other_engines above already
        # freed it; a non-"acquired" result means a live holder slipped in. The
        # mutex is inviolable: rather than run two engines at once, kill the
        # child we just spawned and abort this launch cleanly.
        _lock_result = await asyncio.to_thread(
            _jobs.engine_lock.acquire,
            "briefing",
            _jobs._briefing_proc.pid,
            _jobs._briefing_proc.pid,
            trigger,
        )
        if _lock_result != "acquired":
            _log.warning("briefing.engine_lock_contended_abort", result=_lock_result)
            await _kill_briefing_proc(_jobs._briefing_proc)
            _jobs._briefing_proc = None
            out.close()
            await asyncio.to_thread(_jobs._finish_engine_dag_run, run_id, "cancelled")
            return
        emit_started("briefing")
    except Exception:
        # Setup raised after (or during) the spawn. If a child is live, kill its
        # whole group so it can't keep running unsupervised while we free the
        # engine slot. Then close the fd, release the lock (scoped by pid → a
        # no-op if held by anyone else), and mark the run failed.
        if _jobs._briefing_proc is not None and _jobs._briefing_proc.returncode is None:
            await _kill_briefing_proc(_jobs._briefing_proc)
        out.close()
        if _jobs._briefing_proc is not None:
            await _jobs._release_lock("briefing", _jobs._briefing_proc.pid)
            _jobs._briefing_proc = None
        await asyncio.to_thread(_jobs._finish_engine_dag_run, run_id, "failed")
        raise

    proc = _jobs._briefing_proc

    async def _close_log_on_exit() -> None:
        # ``emit_stopped`` sets the engine-idle event the queue runner blocks on,
        # so it MUST fire even if cleanup throws — otherwise a failing
        # ``out.close()`` (full disk / vanished iCloud-backed log dir) or a
        # raising ``_release_lock`` would strand every later queued engine
        # forever. Mirror the ingestion launcher's finally-guarded emit. Compute
        # status early, make the fragile cleanup steps non-fatal, and emit in a
        # finally.
        status = "failed"
        try:
            if proc:
                await proc.wait()
            status = _jobs._engine_status_from_returncode(proc.returncode if proc else None)
            try:
                out.close()
            except Exception:
                _log.exception("briefing.close_log_failed")
            # Release the engine slot now the child has exited (a kill path may
            # have already released it — release is a scoped no-op then).
            if proc:
                try:
                    await _jobs._release_lock("briefing", proc.pid)
                except Exception:
                    _log.exception("briefing.release_lock_failed")
        finally:
            emit_stopped("briefing", status)
            await asyncio.to_thread(_jobs._finish_engine_dag_run, run_id, status)
            await _jobs._record_engine_run(
                "briefing",
                started_at,
                time.time(),
                status,
                log_slices=[(str(_jobs._KNOWLEDGE_LOG), log_start, "")],
            )

    _jobs._track_background_task(asyncio.create_task(_close_log_on_exit()))
