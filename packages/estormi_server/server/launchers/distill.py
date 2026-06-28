"""Distillation engine launcher.

Mirrors the briefing launcher (own log file, engine lock, history record)
with one twist: the distillation chain is long and cooperative — when the
child notices another engine waiting for the slot it exits with
``YIELD_EXIT_CODE`` (75), and this launcher re-enqueues it at the back of
the queue instead of recording a failure. On-disk checkpoints (harvest,
dataset, adapter) make the resume free. See ``estormi_distill.run_distill``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import structlog

from estormi_server.server import jobs as _jobs
from estormi_server.server.events import emit_started, emit_stopped
from estormi_server.storage.tools import DATA_DIR

log = structlog.get_logger()

_DISTILL_LOG = Path(DATA_DIR) / "logs" / "distill.log"
# The child's "I gave the slot away" exit code (EX_TEMPFAIL) — mirrored from
# estormi_distill.run_distill.YIELD_EXIT_CODE without importing the engine
# package into the server process.
YIELD_EXIT_CODE = 75


async def _distill_running() -> bool:
    if _jobs._distill_proc and _jobs._distill_proc.returncode is None:
        return True
    _jobs._distill_proc = None
    return await _jobs._locked_alive("distill")


async def _kill_distill_proc(proc) -> None:
    """SIGTERM (then SIGKILL) the distill child's whole process group —
    it spawns MLX training/convert grandchildren."""
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


async def _kill_distill() -> None:
    import signal as _signal  # noqa: PLC0415

    proc = _jobs._distill_proc
    pid: int | None = None
    if proc and proc.returncode is None:
        pid = proc.pid
        await _kill_distill_proc(proc)
    else:
        pid = await _jobs._locked_pgid("distill")
        if pid is not None:
            try:
                _jobs.os.killpg(pid, _signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    _jobs._distill_proc = None
    if pid is not None:
        await _jobs._release_lock("distill", pid)


async def _launch_distill(trigger: str = "manual") -> None:
    """Launch the distillation chain under the engine mutex."""
    await _jobs.stop_other_engines("distill")
    env = _jobs.engine_subprocess_env()
    _DISTILL_LOG.parent.mkdir(parents=True, exist_ok=True)
    _jobs._trim_log_history(str(_DISTILL_LOG))
    log_start = _jobs._log_size(str(_DISTILL_LOG))
    run_header = f"\n── run started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──\n".encode()
    out = open(_DISTILL_LOG, "ab", buffering=0)  # noqa: SIM115 — append; history kept across runs
    run_id = await asyncio.to_thread(
        _jobs._start_engine_dag_run, "distill", str(_DISTILL_LOG), trigger
    )
    _log = log.bind(run_id=run_id)
    started_at = time.time()
    try:
        out.write(run_header)
        _jobs._distill_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "estormi_distill.run_distill",
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(_jobs.ROOT),
            start_new_session=True,
        )
        _lock_result = await asyncio.to_thread(
            _jobs.engine_lock.acquire,
            "distill",
            _jobs._distill_proc.pid,
            _jobs._distill_proc.pid,
            trigger,
        )
        if _lock_result != "acquired":
            _log.warning("distill.engine_lock_contended_abort", result=_lock_result)
            await _kill_distill_proc(_jobs._distill_proc)
            _jobs._distill_proc = None
            out.close()
            await asyncio.to_thread(_jobs._finish_engine_dag_run, run_id, "cancelled")
            return
        emit_started("distill")
    except Exception:
        if _jobs._distill_proc is not None and _jobs._distill_proc.returncode is None:
            await _kill_distill_proc(_jobs._distill_proc)
        out.close()
        if _jobs._distill_proc is not None:
            await _jobs._release_lock("distill", _jobs._distill_proc.pid)
            _jobs._distill_proc = None
        await asyncio.to_thread(_jobs._finish_engine_dag_run, run_id, "failed")
        raise

    proc = _jobs._distill_proc

    async def _close_log_on_exit() -> None:
        # ``emit_stopped`` sets the engine-idle event the queue runner blocks on,
        # so it MUST fire even if cleanup throws — otherwise a failing
        # ``out.close()`` or a raising ``_release_lock`` would strand every later
        # queued engine forever. Mirror the ingestion launcher's finally-guarded
        # emit: compute status early, make cleanup non-fatal, emit in a finally.
        status = "failed"
        yielded = False
        try:
            if proc:
                await proc.wait()
            rc = proc.returncode if proc else None
            yielded = rc == YIELD_EXIT_CODE
            # A yield is a clean hand-off, not a failure: record it ok and put
            # the engine back at the tail of the queue so whoever was waiting
            # runs first.
            status = "ok" if yielded else _jobs._engine_status_from_returncode(rc)
            try:
                out.close()
            except Exception:
                _log.exception("distill.close_log_failed")
            if proc:
                try:
                    await _jobs._release_lock("distill", proc.pid)
                except Exception:
                    _log.exception("distill.release_lock_failed")
        finally:
            emit_stopped("distill", status)
            await asyncio.to_thread(_jobs._finish_engine_dag_run, run_id, status)
            await _jobs._record_engine_run(
                "distill",
                started_at,
                time.time(),
                status,
                log_slices=[(str(_DISTILL_LOG), log_start, "")],
            )
            # The engine writes status.json per phase, but a kill/crash leaves the
            # last in-flight phase (e.g. "train") behind with no terminal write —
            # which would strand the Maintenance card on "Training…" forever. Stamp
            # a terminal phase here. ``estormi_distill.paths`` is light (no MLX), so
            # this lazy import doesn't pull the engine into the server process.
            if not yielded and status != "ok":
                try:
                    from estormi_distill.paths import read_status, write_status  # noqa: PLC0415

                    if read_status().get("phase") not in ("done", "rejected", "failed"):
                        write_status(phase="failed", error="stopped before completion")
                except Exception:
                    log.exception("distill.status_reconcile_failed")
            if yielded:
                log.info("distill.yielded_requeueing")
                await _jobs.enqueue("distill", "backlog")

    _jobs._track_background_task(asyncio.create_task(_close_log_on_exit()))
