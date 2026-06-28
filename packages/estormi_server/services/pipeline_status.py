"""Daily ingestion pipeline status — data layer behind the pipeline view.

Reads pipeline run/stage state from the canonical ``dag_runs`` / ``dag_stages``
tables (populated by ``scripts/daily_ingestion.sh`` via the
``memory_core.dag_state`` CLI). ``get_pipeline_data()`` builds the JSON
snapshot served at ``GET /api/pipeline``. The pipeline page itself is now a
Vite SPA that consumes that JSON — the server no longer renders any HTML
here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from estormi_server.server.log_tail import tail_lines
from memory_core import dag_state
from memory_core.settings import DB_PATH, resolve_data_dir

# Single ESTORMI_DATA_DIR contract — applies ``expanduser`` to the override too
# (a value containing ``~`` resolves identically everywhere). See
# ``memory_core.settings.resolve_data_dir``.
_DATA_DIR = Path(resolve_data_dir())
_LOG_DIR = Path(os.getenv("DAG_LOG_DIR", str(_DATA_DIR / "logs")))

DAG_ERR_LOG_CANDIDATES = [_LOG_DIR / "estormi-daily-dag-error.log"]


# Canonical stage order — defines pipeline topology and layout. Mirrors the
# ``dag_stage`` connectors in ``connectors/`` (the list
# ``daily_ingestion.sh`` derives from ``connectors stages``).
#
# Briefing is NOT a pipeline stage: it runs as its own engine, triggered by the
# server watcher under the engine mutex in ``server/jobs.py`` — not chained off
# the nightly source pipeline. Correlation is not an engine at all: it is emergent
# from time-window retrieval (the ``fetch_around`` tool).
#
# ``gcal`` (Google Calendar) ingests calendar events as ``personal``-corpus
# chunks. It is an opt-in stage (``default_stage=False``) — it runs only once the
# user has wired Google OAuth.
def _derived_dag_stages() -> list[str]:
    """Read pipeline stages from the connector registry.

    The hardcoded list this replaced silently dropped new connectors from
    the UI's "Last 14 runs" strip and the ``enriched_stages`` pending
    rendering until someone remembered to edit ``pipeline.py``. Deriving
    from ``connectors.dag_stages`` makes the connector registry the single
    source of truth — see the docstring on that function.
    """
    from connectors import dag_stages  # noqa: PLC0415

    return [s.name for s in dag_stages()]


DAG_STAGES = _derived_dag_stages()

# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class StageRun:
    name: str
    status: str  # "ok" | "fail" | "running" | "pending"
    duration_s: int | None = None
    offset_s: int | None = None  # seconds from pipeline start
    log_file: str | None = None


@dataclass
class DagRun:
    started_at: datetime
    log_file: str = ""
    stages: list[StageRun] = field(default_factory=list)
    total_s: int | None = None
    is_running: bool = True
    stage_starts: dict[str, int] = field(default_factory=dict)
    stage_logs: dict[str, str] = field(default_factory=dict)
    # Raw ``dag_runs.status``. We keep it alongside the render-mapped stage
    # statuses so ``overall_status`` can distinguish a cancelled (preempted)
    # run from a clean ok run when neither has any failed stages.
    db_status: str = "ok"

    @property
    def ended_at(self) -> datetime | None:
        if self.total_s is not None:
            return self.started_at + timedelta(seconds=self.total_s)
        return None

    @property
    def failed_stages(self) -> list[str]:
        return [s.name for s in self.stages if s.status == "fail"]

    @property
    def overall_status(self) -> str:
        if self.is_running:
            return "running"
        if self.failed_stages:
            return "fail"
        if self.db_status == "cancelled":
            return "cancelled"
        return "ok"

    def enriched_stages(self) -> list[StageRun]:
        """Every canonical stage with inferred status (running / pending)."""
        done = {s.name: s for s in self.stages}
        result: list[StageRun] = []
        for name in DAG_STAGES:
            if name in done:
                result.append(done[name])
            elif self.is_running:
                # A stage is running only once it has logged its start; with
                # bounded parallelism several may be running at once. Stages not
                # yet launched are pending.
                if name in self.stage_starts:
                    result.append(
                        StageRun(
                            name=name,
                            status="running",
                            offset_s=self.stage_starts[name],
                            log_file=self.stage_logs.get(name),
                        )
                    )
                else:
                    result.append(StageRun(name=name, status="pending"))
            else:
                result.append(StageRun(name=name, status="pending"))
        return result


# ── DB → DagRun adapter ──────────────────────────────────────────────────────


def _dag_process_alive() -> bool:
    """True if an ingestion pipeline is actually running.

    Reads the cross-process engine lock (signal-0 probed, restart-surviving)
    instead of scanning the process table with ``pgrep`` — the lock is the
    single source of truth for "is the pipeline live" now.
    """
    from memory_core import engine_lock  # noqa: PLC0415

    row = engine_lock.current()
    return row is not None and row.kind == "ingestion" and engine_lock.is_alive(row)


# Map dag_stages.status (ok/skipped/failed/running/cancelled) → render status.
# "skipped" renders as ok visually (stage chose not to run). "cancelled" is a
# preempted-stage marker (killed by stop/restart, no real failure) — the SPA
# renders it neutrally rather than as red "FAILED".
_DB_STATUS_TO_RENDER = {
    "ok": "ok",
    "skipped": "ok",
    "failed": "fail",
    "fail": "fail",  # legacy/backfill rows
    "running": "running",
    "cancelled": "cancelled",
}


def _row_to_dag_run(row: dag_state.DagRunRow) -> DagRun:
    """Build the UI-facing ``DagRun`` from a ``dag_state.DagRunRow``."""
    started = row.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)

    is_running = row.status == "running"
    total_s: int | None
    if row.duration_ms is not None:
        total_s = max(0, int(row.duration_ms // 1000))
    elif row.ended_at is not None:
        total_s = max(0, int((row.ended_at - started).total_seconds()))
    else:
        total_s = None

    stage_starts: dict[str, int] = {}
    stage_logs: dict[str, str] = {}
    stages: list[StageRun] = []
    for s in row.stages:
        s_start = s.started_at
        if s_start.tzinfo is None:
            s_start = s_start.replace(tzinfo=timezone.utc)
        offset_s = max(0, int((s_start - started).total_seconds()))
        stage_starts[s.stage_name] = offset_s
        if s.log_path:
            stage_logs[s.stage_name] = s.log_path

        render_status = _DB_STATUS_TO_RENDER.get(s.status, s.status)
        if s.duration_ms is not None:
            dur_s: int | None = max(0, int(s.duration_ms // 1000))
        elif s.ended_at is not None:
            end = s.ended_at
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            dur_s = max(0, int((end - s_start).total_seconds()))
        else:
            dur_s = None

        # Only emit a finished StageRun row for terminal statuses; running
        # stages flow through ``enriched_stages`` via stage_starts.
        if render_status != "running":
            stages.append(
                StageRun(
                    name=s.stage_name,
                    status=render_status,
                    duration_s=dur_s,
                    offset_s=offset_s,
                    log_file=s.log_path,
                )
            )

    return DagRun(
        started_at=started,
        log_file=row.log_path or "",
        stages=stages,
        total_s=total_s,
        is_running=is_running,
        stage_starts=stage_starts,
        stage_logs=stage_logs,
        db_status=row.status,
    )


def _parse_dag_log() -> list[DagRun]:
    """Return ingestion pipeline runs (oldest → newest) for the UI to render.

    Backed by the canonical ``dag_runs`` / ``dag_stages`` tables (managed by
    ``scripts/daily_ingestion.sh`` via the ``memory_core.dag_state`` CLI).

    Scoped to ``engine='ingestion'``: briefing runs share the same tables but
    carry no pipeline stages, so an unfiltered query let the newest briefing land as
    ``runs[-1]`` — making ``last`` a stage-less run that rendered every stage as
    "pending" and reported the briefing's timing as the pipeline's last run.
    """
    try:
        rows = dag_state.get_recent_runs(limit=20, engine="ingestion")
    except Exception:
        return []  # best-effort: pipeline tables unavailable, render empty history
    # dag_state returns newest-first; the rest of pipeline.py assumes
    # oldest → newest order (``runs[-1]`` is the most recent run).
    rows = list(reversed(rows))
    runs = [_row_to_dag_run(row) for row in rows]
    if runs and runs[-1].is_running and not _dag_process_alive():
        # No live process and still ``running`` in DB → crashed/killed.
        runs[-1].is_running = False
    return runs


# Canonical default schedule (settings key → cron) for each engine — one source
# of truth so lifespan boot, the wake catch-up (schedulers), and the status
# fallback below can't drift. The live cron is read from the ``settings`` table
# at boot; these defaults apply only when a key is absent.
ENGINE_SCHEDULE_DEFAULTS: dict[str, str] = {
    "schedule_cron": "0 2 * * *",  # ingestion — 02:00 daily
    "briefing_schedule_cron": "0 7 * * *",  # briefing — 07:00 daily
    "distill_schedule_cron": "0 3 * * 0",  # distill — Sunday 03:00 weekly
}

# Kept in sync by main.py on startup and whenever put_settings changes the cron.
_schedule_cron: str = ENGINE_SCHEDULE_DEFAULTS["schedule_cron"]


def set_schedule_cron(cron: str) -> None:
    global _schedule_cron
    _schedule_cron = cron


def _next_run_at(cron: str | None = None) -> datetime | None:
    effective = cron if cron is not None else _schedule_cron
    if effective == "manual":
        return None
    try:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(effective)
        return trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    except Exception:
        return None  # best-effort: unparseable cron yields no scheduled time


def _fmt_local(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _fmt_duration(s: int | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _time_ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = int((now - dt).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _recent_errors() -> list[str]:
    try:
        for err_log in DAG_ERR_LOG_CANDIDATES:
            if err_log.exists():
                text = tail_lines(err_log, 20)
                return [ln for ln in text.splitlines() if ln.strip()]
    except OSError:
        pass
    return []


def _chunks_added_since(since: datetime) -> tuple[int, dict[str, int]]:
    """Chunks ingested since ``since`` (UTC), as ``(total, by_source)``.

    Proxy for "how many chunks did the last pipeline run add" — runs are serialised
    by the engine mutex, so anything in ``chunks`` newer than the run's start
    is attributable to that run. Returns ``(0, {})`` on any DB error so a
    transient SQLite hiccup never breaks ``/api/pipeline``.
    """
    import sqlite3  # noqa: PLC0415

    db_path = os.getenv("ESTORMI_DB_PATH", DB_PATH)
    iso = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Read-only URI + short timeout: this counter sits on `/api/pipeline`'s
    # hot path. If the engine holds the writer lock, returning (0, {}) so the
    # UI shows "0 chunks added" is far better than stalling the poll for 5 s.
    # The next poll naturally catches up.
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=0.3, isolation_level=None
        )
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT source, COUNT(*) AS n FROM chunks WHERE ingested_at >= ? GROUP BY source",
                (iso,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, {}
    by_source = {r["source"] or "unknown": int(r["n"]) for r in rows}
    return sum(by_source.values()), by_source


def get_pipeline_data() -> dict[str, Any]:
    runs = _parse_dag_log()
    completed = [r for r in runs if not r.is_running]
    last = runs[-1] if runs else None
    durations = [r.total_s for r in completed if r.total_s is not None]
    mean_s = int(mean(durations)) if len(durations) >= 2 else (durations[0] if durations else None)
    last_chunks_total, last_chunks_by_source = (
        _chunks_added_since(last.started_at) if last else (0, {})
    )
    return {
        "is_running": last.is_running if last else False,
        "overall_status": last.overall_status if last else "unknown",
        "last_run_started": _fmt_local(last.started_at) if last else None,
        "last_run_ended": _fmt_local(last.ended_at) if last else None,
        "last_run_duration_s": last.total_s if last else None,
        "last_run_duration": _fmt_duration(last.total_s if last else None),
        "last_run_ago": _time_ago(last.started_at) if last else "never",
        "last_run_failed_stages": last.failed_stages if last else [],
        # Chunks ingested since the last run started — total + per-source.
        # Surfaces "what did the last run add" in the hero without needing
        # per-stage instrumentation: the engine mutex serialises runs, so
        # rows with ``ingested_at >= started_at`` belong to that run.
        "last_run_chunks_added": last_chunks_total,
        "last_run_chunks_by_source": last_chunks_by_source,
        "mean_duration_s": mean_s,
        "mean_duration": _fmt_duration(mean_s),
        "next_run_at": _fmt_local(_next_run_at()),
        "run_count": len(completed),
        "errors": _recent_errors(),
        # `enriched_stages()` includes the currently-running stage (the one
        # that has logged a start but not an end) so the SPA can paint it
        # yellow + blinking. Falling back to `last.stages` would only list
        # already-completed stages and leave the active one invisible to
        # the UI mid-run.
        "stages": [
            {
                "name": s.name,
                "status": s.status,
                "duration_s": s.duration_s,
                "duration": _fmt_duration(s.duration_s),
                # Wall-clock start of THIS stage, epoch ms. Populated for the
                # running stage so the SPA can grow its timeline bar live
                # between polls (the 5 s `/api/pipeline` poll alone would
                # make the bar advance in coarse 5 s steps).
                "started_at_epoch_ms": (
                    int((last.started_at.timestamp() + (s.offset_s or 0)) * 1000)
                    if (last and s.status == "running" and s.offset_s is not None)
                    else None
                ),
            }
            for s in (last.enriched_stages() if last else [])
        ],
        "history": [
            {
                "started_at": _fmt_local(r.started_at),
                "duration": _fmt_duration(r.total_s),
                "duration_s": r.total_s,
                "status": r.overall_status,
                "failed_stages": r.failed_stages,
                "log_file": r.log_file,
                # Per-stage log paths for THIS run so a click on a single
                # rectangle in the "Last 14 days" strip can open the right
                # stage log instead of the pipeline-level log. Keys are the
                # canonical pipeline stage names (`notes`, `mail`, …).
                "stage_logs": r.stage_logs,
                # Per-stage final status for this run — same canonical
                # keys — so the SPA can colour each rectangle correctly
                # even for older runs whose stage list has aged out.
                "stage_statuses": {s.name: s.status for s in r.stages},
                # Per-stage duration + start offset for THIS run, so the
                # modal opened from a historic-strip rectangle shows the
                # stage's own duration/started values instead of falling
                # back to the run-level duration with an empty "Started".
                "stage_durations": {s.name: _fmt_duration(s.duration_s) for s in r.stages},
                "stage_durations_s": {
                    s.name: s.duration_s for s in r.stages if s.duration_s is not None
                },
                "stage_offsets_s": {s.name: s.offset_s for s in r.stages if s.offset_s is not None},
            }
            for r in reversed(runs[-14:])
        ],
    }
