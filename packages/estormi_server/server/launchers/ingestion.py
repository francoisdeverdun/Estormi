"""Ingestion pipeline launcher + stage gating.

Shared state (``_dag_proc``, ``_dag_pgid``, ``_dag_lock``, the ``_DAG_*_LOG``
paths and the engine-lock helpers ``_locked_pgid`` / ``_release_lock``), the
``ROOT`` repo root and the ``stop_other_engines`` mutex all live in
``server.jobs``; we reach back through that module so test patches on
``server.jobs.<name>`` (``_DAG_MAIN_LOG``, ``ROOT``, ``os.killpg``,
``asyncio.sleep``, ``_settings_snapshot``, ``stop_other_engines``) still drive
this code path.

The connector-registry-derived constants (``_STAGE_KEY_TO_DAG``,
``_ROOT_REQUIRED_STAGES``, ``_DEPTH_ENV``) are computed here on import so
they can't drift from the registry; ``server.jobs`` re-exports them for
external readers.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import structlog

from estormi_server.server import jobs as _jobs
from estormi_server.server.events import emit_started, emit_stopped

log = structlog.get_logger()


# ─── Registry-derived stage/source maps ──────────────────────────────────────
# The connector registry (connectors) is the single source of truth
# for which pipeline stages exist and which env var each depth-capable source
# reads. These maps used to be hardcoded and drifted out of sync with the
# registry; now they are derived from it on import.
def _load_connector_registry():
    """Import the connector registry, making the repo root importable first."""
    import sys as _sys  # noqa: PLC0415

    root = str(_jobs.ROOT)
    if root not in _sys.path:
        _sys.path.insert(0, root)
    from connectors import registry as _registry  # noqa: PLC0415

    return _registry


_registry = _load_connector_registry()

# Every pipeline stage, in execution order. Identity map (stage key == DAG name)
# since the canonical-naming refactor — kept as a dict for the existing
# `_run_dag` call sites that iterate `key, dag_name`.
_STAGE_KEY_TO_DAG = {
    s.name: s.name
    for s in sorted(
        (sp for sp in _registry.specs() if sp.dag_stage),
        key=lambda sp: sp.dag_order,
    )
}

# Stage keys whose connector needs an explicit filesystem root (`<key>_root`
# in settings) before it can ingest. Until that root is set the pipeline skips
# the stage — launching the ingester rootless just burns a no-op run and
# lights up the timeline for a source the Sources UI already shows as
# "Disabled". Derived from the registry so it can't drift.
_ROOT_REQUIRED_STAGES = frozenset(
    s.name for s in _registry.specs() if s.dag_stage and s.requires_root
)

_DEPTH_TO_DAYS = {
    # Short windows for news-style sources (knowledge): week/month granularity.
    "1w": "7",
    "2w": "14",
    "1m": "30",
    "3m": "90",
    # Longer windows for the personal sources (notes, mail, …).
    "90d": "90",
    "6m": "180",
    "1y": "365",
    "2y": "730",
    "all": "36500",
}

# Universal default historic depth — applied to every depth-capable source
# that has no explicit pick in the Manage modal and no per-source default.
_DEFAULT_DEPTH = "90d"

# Source key → the env var its ingest script reads for the first-run history
# window, derived from each connector spec's `depth_window_env`. Only
# depth-capable sources declare one; the rest (reminders, whatsapp,
# documents) ingest everything available and the Manage modal hides the
# depth picker for them.
_DEPTH_ENV = {s.name: s.depth_window_env for s in _registry.specs() if s.depth_window_env}

# Source key → its own default depth token (e.g. ``knowledge`` → ``1w``),
# derived from each spec's `default_depth`. Sources without one fall back to
# ``_DEFAULT_DEPTH``. Lets a news source default to a short window while the
# personal sources keep the 90-day default.
_DEPTH_DEFAULTS = {
    s.name: s.default_depth for s in _registry.specs() if s.depth_window_env and s.default_depth
}


def apply_ingest_env_overrides(env: dict[str, str], settings: dict[str, str]) -> None:
    """Mutates `env` with the source-config overrides every ingest needs.

    Both ``_run_dag`` (Run All / scoped DAG) and the per-source ingest
    endpoint (``/api/sources/{name}/ingest`` — what the ▶ play button
    used to call directly) must apply the same translations or the
    user's historic_depth choice silently doesn't take effect on one
    path while working on the other. The trio:

      * ``DOCUMENTS_ROOT`` — the folder-rooted source needs it to know
        where to walk.
      * the first-run history window — derived from the modal's
        historic-depth picker and written to whichever env var the
        source's script reads (see ``_DEPTH_ENV``). Only depth-capable
        sources get one; the rest ingest everything available.
    """
    documents_root = settings.get("documents_root", "")
    if documents_root:
        env["DOCUMENTS_ROOT"] = documents_root
    for key, env_var in _DEPTH_ENV.items():
        depth = (settings.get(f"{key}_historic_depth", "") or "").lower().strip()
        # Always set the window — an unset/unknown pick falls back to the
        # source's own default depth, then the universal default, so first
        # runs never grab everything.
        fallback = _DEPTH_DEFAULTS.get(key, _DEFAULT_DEPTH)
        env[env_var] = (
            _DEPTH_TO_DAYS.get(depth)
            or _DEPTH_TO_DAYS.get(fallback)
            or _DEPTH_TO_DAYS[_DEFAULT_DEPTH]
        )


async def _settings_snapshot() -> dict[str, str]:
    """Read every settings row at once. Cheap (SQLite is local) and lets
    callers pass a consistent map into ``apply_ingest_env_overrides``."""
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    db = sqlite_conn()
    cur = await db.execute("SELECT key, value FROM settings")
    rows = await cur.fetchall()
    await cur.close()
    return {row[0]: row[1] for row in rows}


def _stage_runnable(key: str, settings: dict[str, str]) -> bool:
    """Whether a pipeline stage may run given the current settings.

    Two gates, both must pass:

      * the on/off toggle (``source_<key>_enabled``) — records user intent;
      * for a root-required source (documents, code), a non-empty
        ``<key>_root`` — without it the ingester bails immediately, so the
        stage is "enabled but not set up". Skipping it here keeps the pipeline
        from launching a no-op run for a source the UI shows as "Disabled".
    """
    toggle = settings.get(f"source_{key}_enabled", "false") or "false"
    if toggle in ("false", ""):
        return False
    if key in _ROOT_REQUIRED_STAGES:
        return bool((settings.get(f"{key}_root", "") or "").strip())
    return True


def _killpg_alive(pgid: int) -> bool:
    """True if any process in ``pgid`` still exists. Uses signal 0."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Can't probe — assume alive so the caller doesn't unlink the pid
        # file under a still-running pipeline owned by another uid.
        return True
    except OSError:
        return False


async def _kill_dag_processes() -> None:
    """Stop the daily-ingestion pipeline by its recorded process group.

    Targets the PGID the engine lock records (the shell script's group), falling
    back to the in-process handles. Escalates SIGTERM → SIGKILL: a worker that
    ignores SIGTERM (or is stuck in a blocking syscall) would otherwise survive
    while still writing into tables, so we confirm the group is gone before
    releasing the lock.
    """
    import signal as _signal  # noqa: PLC0415

    # The engine lock is the authoritative record of the *currently live*
    # pipeline — the shell script acquires it at launch and releases it in its
    # EXIT trap (it replaced the /tmp pid file). An in-memory _dag_pgid can be
    # stale from an earlier run, so prefer the lock; fall back to the in-process
    # handles for the brief window before the shell script has acquired.
    pgid: int | None = await _jobs._locked_pgid("ingestion")
    if pgid is None:
        pgid = _jobs._dag_pgid
    if pgid is None and _jobs._dag_proc and _jobs._dag_proc.returncode is None:
        pgid = _jobs._dag_proc.pid
    killpg_denied = False
    if pgid is not None:
        try:
            _jobs.os.killpg(pgid, _signal.SIGTERM)
        except ProcessLookupError:
            pgid = None  # already gone — pid file is stale, safe to clear
        except PermissionError:
            # We can't kill this PGID (different uid / cross-user run).
            # The pipeline it points to is still alive; unlinking the pid file
            # would leave it running with no record, so we bail.
            killpg_denied = True
            log.warning("dag.kill.permission_denied", pgid=pgid)
        except OSError:
            pass
    # Escalation: poll briefly, then SIGKILL if anything in the group is
    # still alive. Without this a worker that traps SIGTERM (or is wedged
    # in I/O) survives, but the code below would still unlink the pid file
    # and pretend the pipeline had stopped.
    if pgid is not None and not killpg_denied:
        for _ in range(30):  # ~3 s budget at 100 ms
            if not _killpg_alive(pgid):
                break
            await _jobs.asyncio.sleep(0.1)
        else:
            try:
                _jobs.os.killpg(pgid, _signal.SIGKILL)
                log.warning("dag.kill.sigterm_escalated_to_sigkill", pgid=pgid)
            except ProcessLookupError:
                pass
            except OSError as exc:
                log.warning("dag.kill.sigkill_failed", pgid=pgid, error=str(exc))
    if not killpg_denied:
        # Free the slot immediately rather than waiting for steal-if-dead. Scoped
        # to the killed group's id (== the shell script's recorded pid) so a
        # concurrent briefing lock is never dropped; a mismatch just no-ops and
        # steal-if-dead reclaims it later.
        if pgid is not None:
            await _jobs._release_lock("ingestion", pgid)
        _jobs._dag_proc = None
        _jobs._dag_pgid = None


async def _run_dag(stage_override: str | None = None) -> None:
    """Run the daily ingestion pipeline.

    ``stage_override`` (when set) limits the run to that single stage so
    the per-source ▶ play button can drive the same pipeline state machine as
    Run All. The selected stage must still be runnable (see
    ``_stage_runnable``) — we reject an attempt to run a stage the user has
    toggled off or that is still awaiting setup.
    """
    if _jobs._dag_lock.locked():
        log.warning("dag.skipped.already_running")
        return
    # ``emit_stopped`` sets the engine-idle event the queue runner waits on. If
    # it fired while we still held ``_dag_lock``, the runner could wake, pop the
    # next ingestion entry, and have *its* ``_run_dag`` bounce off the still-held
    # lock (``dag.skipped.already_running``) — silently dropping a queued run. So
    # capture the run outcome inside the lock but emit + record AFTER releasing.
    status: str | None = None
    end_at = 0.0
    started_at = 0.0
    log_slices: list = []
    try:
        async with _jobs._dag_lock:
            env = os.environ.copy()
            # Stream stage logs live: without this, Python stages (gcal,
            # documents, briefing) block-buffer stdout when it's a file, so the
            # UI's per-stage Log box stays empty until the stage exits.
            env["PYTHONUNBUFFERED"] = "1"
            settings = await _jobs._settings_snapshot()
            apply_ingest_env_overrides(env, settings)
            enabled_stages = [
                dag_name
                for key, dag_name in _STAGE_KEY_TO_DAG.items()
                if _stage_runnable(key, settings)
            ]
            # Decide whether there is actually something to run BEFORE preempting
            # other engines. Earlier the kill was unconditional, so clicking ▶
            # with no enabled sources (or with a disabled stage_override) silently
            # SIGTERM'd a running briefing run.
            if not enabled_stages:
                log.info("dag.skipped.no_sources_enabled")
                return
            if stage_override:
                # The play button on a single source row hits us with the
                # canonical pipeline stage name (e.g. "notes", "whatsapp").
                # Honour it only when that source is runnable — running a
                # disabled or not-yet-configured stage would either fail or
                # surprise the user.
                if stage_override not in enabled_stages:
                    toggle_on = (
                        settings.get(f"source_{stage_override}_enabled", "false") or "false"
                    ) not in ("false", "")
                    if toggle_on and stage_override in _ROOT_REQUIRED_STAGES:
                        log.warning(
                            "dag.skipped.stage_awaiting_setup",
                            stage=stage_override,
                            reason="filesystem root not configured",
                        )
                    else:
                        log.warning(
                            "dag.skipped.stage_not_enabled",
                            stage=stage_override,
                            enabled=enabled_stages,
                        )
                    return
                env["STAGES"] = stage_override
            else:
                env["STAGES"] = " ".join(enabled_stages)

            # Engine mutex: a pipeline run — scheduled or manual — preempts whatever
            # else is running. Exactly one engine works at a time. Deferred until
            # after the runnability checks so a no-op call doesn't kill engines.
            await _jobs.stop_other_engines("ingestion")
            Path(_jobs._DAG_MAIN_LOG).parent.mkdir(parents=True, exist_ok=True)
            # Trim before the open(..., "a") opens an inherited fd; otherwise
            # the pipeline appends straight onto an unbounded history file. Matches
            # what the briefing launcher already does.
            _jobs._trim_log_history(str(_jobs._DAG_MAIN_LOG))
            _jobs._trim_log_history(str(_jobs._DAG_ERR_LOG))
            # Snapshot each log's length now so the end-of-run record captures only
            # this run's appended output (the files are shared across runs).
            out_log_start = _jobs._log_size(str(_jobs._DAG_MAIN_LOG))
            err_log_start = _jobs._log_size(str(_jobs._DAG_ERR_LOG))
            started_at = time.time()
            with (
                open(_jobs._DAG_MAIN_LOG, "a") as dag_stdout,
                open(_jobs._DAG_ERR_LOG, "a") as dag_stderr,
            ):
                proc = await asyncio.create_subprocess_exec(
                    "bash",
                    str(_jobs.ROOT / "scripts" / "daily_ingestion.sh"),
                    stdout=dag_stdout,
                    stderr=dag_stderr,
                    env=env,
                    start_new_session=True,
                    cwd=str(_jobs.ROOT),
                )
                _jobs._dag_proc = proc
                # with start_new_session=True, PGID == PID; capture now while alive
                _jobs._dag_pgid = proc.pid
                emit_started("ingestion")
                try:
                    await proc.wait()
                finally:
                    # Always clear globals so a stale PID can't be killed later (it
                    # may have been recycled by the OS). Capture the outcome here,
                    # but emit/record below — see the outer finally.
                    _jobs._dag_proc = None
                    _jobs._dag_pgid = None
                    status = _jobs._engine_status_from_returncode(proc.returncode)
                    end_at = time.time()
                    log_slices = [
                        (str(_jobs._DAG_MAIN_LOG), out_log_start, ""),
                        (str(_jobs._DAG_ERR_LOG), err_log_start, "stderr"),
                    ]
            log.info("dag.finished", returncode=proc.returncode)
            # No engine is chained off the pipeline: the briefing engine runs on its own
            # cron and reads the freshly ingested chunks via time-window retrieval.
    finally:
        # Lock released. Signal engine-idle + persist the run record now, so a
        # queued ingestion run waking on the idle event finds the lock free
        # instead of bouncing off it (the dropped-run race). Guarded on status:
        # the early-return paths (no sources / stage not runnable) never started
        # an engine, so there's nothing to stop or record.
        if status is not None:
            emit_stopped("ingestion", status)
            await _jobs._record_engine_run(
                "ingestion",
                started_at,
                end_at,
                status,
                log_slices=log_slices,
            )
