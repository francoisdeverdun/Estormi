"""Canonical DAG-run / DAG-stage state — first-class SQLite.

Replaces the regex-based log parsing in ``estormi_server/services/pipeline_status.py`` for
reconstructing run history. ``scripts/daily_ingestion.sh`` records lifecycle
events through the CLI in this module; the pipeline API/UI reads from these
two tables (``dag_runs``, ``dag_stages``) instead of tail-parsing the launchd
stdout log.

Public API:
    start_run(trigger, log_path, err_path, started_at=None) -> int
    finish_run(run_id, status, duration_ms=None, ended_at=None) -> None
    start_stage(run_id, stage_name, log_path, started_at=None) -> int
    finish_stage(stage_id, status, exit_code=None, duration_ms=None,
                 stderr_excerpt=None, ended_at=None) -> None
    get_recent_runs(limit=20) -> list[DagRunRow]
    get_run(run_id) -> DagRunRow | None
    reconcile_orphaned_runs(pid_file_exists=False, engine="ingestion") -> None
    db_path() -> str

CLI:
    python -m memory_core.dag_state start-run --trigger ... --log-path ... \\
        --err-path ...
    python -m memory_core.dag_state finish-run --run-id ... --status ...
    python -m memory_core.dag_state start-stage --run-id ... --stage ... \\
        --log-path ...
    python -m memory_core.dag_state finish-stage --stage-id ... --status ... \\
        [--exit-code ...]

The CLI is invoked from ``scripts/daily_ingestion.sh`` between stage steps.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .settings import DB_PATH
from .timeparse import now_iso, parse_iso

# ── Schema ───────────────────────────────────────────────────────────────────

# Canonical DDL for the DAG-state tables. This module is the single source of
# truth for ``dag_runs`` / ``dag_stages`` — ``INIT_SQL`` does NOT declare
# them; startup applies this schema via ``ensure_schema`` (see
# ``estormi_server/server/lifespan.py`` and ``estormi_server/storage/chunk_admin.py``).
DAG_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dag_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    duration_ms INTEGER,
    trigger     TEXT,
    log_path    TEXT,
    err_path    TEXT,
    notes       TEXT,
    engine      TEXT NOT NULL DEFAULT 'ingestion'
);
CREATE INDEX IF NOT EXISTS dag_runs_started_at_idx ON dag_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS dag_runs_engine_idx ON dag_runs(engine, id DESC);

CREATE TABLE IF NOT EXISTS dag_stages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES dag_runs(id) ON DELETE CASCADE,
    stage_name     TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    duration_ms    INTEGER,
    log_path       TEXT,
    exit_code      INTEGER,
    stderr_excerpt TEXT
);
CREATE INDEX IF NOT EXISTS dag_stages_run_idx ON dag_stages(run_id);
"""


async def ensure_schema(conn) -> None:
    """Apply the DAG-state DDL to an ``aiosqlite.Connection``.

    Idempotent. The app's
    ``INIT_SQL`` doesn't declare these tables; startup calls this once so the
    schema is guaranteed before any read/write.
    """
    # ``engine`` was added after the original schema shipped — ALTER it in
    # FIRST, before executescript, so the trailing
    # ``CREATE INDEX dag_runs_engine_idx`` in the DDL can succeed on a DB
    # whose ``dag_runs`` predates the column. CREATE TABLE IF NOT EXISTS
    # would otherwise be a no-op (table already exists) and the index DDL
    # would crash with "no such column: engine".
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dag_runs'"
    )
    have_dag_runs = bool(await cur.fetchone())
    await cur.close()
    if have_dag_runs:
        cur = await conn.execute("PRAGMA table_info(dag_runs)")
        cols = {row[1] for row in await cur.fetchall()}
        await cur.close()
        if "engine" not in cols:
            await conn.execute(
                "ALTER TABLE dag_runs ADD COLUMN engine TEXT NOT NULL DEFAULT 'ingestion'"
            )
            await conn.commit()
    await conn.executescript(DAG_STATE_SCHEMA)
    await conn.commit()


def _ensure_schema_sync(conn: sqlite3.Connection) -> None:
    """Synchronous variant used by the CLI before any reads/writes.

    The shell-script CLI (``scripts/daily_ingestion.sh``) may run before the
    server has applied the schema (fresh install, or a brand-new tmp DB in
    tests), so the sync paths apply the DDL themselves.
    """
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dag_runs'")
    have_dag_runs = bool(cur.fetchone())
    if have_dag_runs:
        cur = conn.execute("PRAGMA table_info(dag_runs)")
        cols = {row[1] for row in cur.fetchall()}
        if "engine" not in cols:
            conn.execute("ALTER TABLE dag_runs ADD COLUMN engine TEXT NOT NULL DEFAULT 'ingestion'")
            conn.commit()
    conn.executescript(DAG_STATE_SCHEMA)
    conn.commit()


# ── Connection helper ────────────────────────────────────────────────────────

# Default DB path. Tests monkeypatch ``DB_PATH_OVERRIDE`` to point at a tmp file.
DB_PATH_OVERRIDE: str | None = None


def db_path() -> str:
    """Resolve the live Estormi DB path.

    Honours the in-process ``DB_PATH_OVERRIDE`` (set by the test suite) and the
    ``ESTORMI_DB_PATH`` env var, falling back to the packaged default. Public so
    out-of-tree readers (e.g. the connector permission gate) can locate the same
    DB without reaching into a private symbol.
    """
    return DB_PATH_OVERRIDE or os.getenv("ESTORMI_DB_PATH", DB_PATH)


# Schema is process-idempotent: once applied against a given DB path, the
# CLI/server combination won't drop or alter the DAG-state tables out from
# under us. Tracking per-path lets the test suite point at a tmp DB without
# inheriting a stale "already applied" flag from a previous test.
_SCHEMA_APPLIED_PATHS: set[str] = set()


def _connect() -> sqlite3.Connection:
    """Open a synchronous sqlite3 connection to the live Estormi DB.

    WAL mode means async writers (the FastAPI process) and this sync reader/
    writer (the shell-script CLI) can coexist. ``busy_timeout=30000`` lets a
    statement wait up to 30s for a competing writer's lock to clear instead of
    failing immediately with ``SQLITE_BUSY`` — the engine pipeline's bursts of
    contention are short, so waiting is preferable to surfacing a spurious
    "database is locked" error to the CLI. Applies the DAG-state schema on first
    connection per DB path so the CLI works even when the server hasn't run yet;
    subsequent connections in the same process skip the DDL probe.

    Callers must CLOSE the connection — ``with closing(_connect()) as conn,
    conn:`` — because ``with conn:`` alone is only sqlite3's transaction
    scope; it leaks the file descriptor (the long-lived server process used
    to accumulate one open DB handle per dag-state call).
    """
    path = db_path()
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if path not in _SCHEMA_APPLIED_PATHS:
        _ensure_schema_sync(conn)
        _SCHEMA_APPLIED_PATHS.add(path)
    return conn


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class DagStageRow:
    id: int
    run_id: int
    stage_name: str
    started_at: datetime
    ended_at: datetime | None
    status: str  # running | ok | skipped | failed | cancelled
    duration_ms: int | None
    log_path: str | None
    exit_code: int | None
    stderr_excerpt: str | None


@dataclass
class DagRunRow:
    id: int
    started_at: datetime
    ended_at: datetime | None
    status: str  # running | ok | failed | cancelled
    duration_ms: int | None
    trigger: str | None
    log_path: str | None
    err_path: str | None
    notes: str | None
    # 'ingestion' | 'briefing' | 'distill'
    engine: str = "ingestion"
    stages: list[DagStageRow] = field(default_factory=list)


def _row_to_run(row: sqlite3.Row, stages: list[DagStageRow]) -> DagRunRow:
    # ``engine`` is absent on a DB created before the migration's first run —
    # row lookup raises IndexError on missing columns. Fall back to the
    # default the migration would set.
    try:
        engine = row["engine"] or "ingestion"
    except (KeyError, IndexError):
        engine = "ingestion"
    return DagRunRow(
        id=row["id"],
        started_at=parse_iso(row["started_at"]) or datetime.now(timezone.utc),
        ended_at=parse_iso(row["ended_at"]),
        status=row["status"],
        duration_ms=row["duration_ms"],
        trigger=row["trigger"],
        log_path=row["log_path"],
        err_path=row["err_path"],
        notes=row["notes"],
        engine=engine,
        stages=stages,
    )


def _row_to_stage(row: sqlite3.Row) -> DagStageRow:
    return DagStageRow(
        id=row["id"],
        run_id=row["run_id"],
        stage_name=row["stage_name"],
        started_at=parse_iso(row["started_at"]) or datetime.now(timezone.utc),
        ended_at=parse_iso(row["ended_at"]),
        status=row["status"],
        duration_ms=row["duration_ms"],
        log_path=row["log_path"],
        exit_code=row["exit_code"],
        stderr_excerpt=row["stderr_excerpt"],
    )


# ── Write API ────────────────────────────────────────────────────────────────


def start_run(
    trigger: str,
    log_path: str,
    err_path: str,
    started_at: str | None = None,
    engine: str = "ingestion",
) -> int:
    """Insert a new DAG run row. Returns the new run_id."""
    started = started_at or now_iso()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO dag_runs (started_at, status, trigger, log_path, err_path, engine) "
            "VALUES (?, 'running', ?, ?, ?, ?)",
            (started, trigger, log_path, err_path, engine),
        )
        conn.commit()
        # lastrowid is non-None after a successful INSERT; coalesce for the type.
        return int(cur.lastrowid or 0)


def finish_run(
    run_id: int,
    status: str,
    duration_ms: int | None = None,
    ended_at: str | None = None,
) -> None:
    """Mark a run finished. ``status`` ∈ {ok, failed, cancelled}.

    Idempotent: a late SIGTERM trap firing after the run already finished
    cleanly must not overwrite an ``ok`` row to ``cancelled``. We guard the
    UPDATE on ``status = 'running'`` so any second call is a no-op.
    """
    ended = ended_at or now_iso()
    with closing(_connect()) as conn, conn:
        if duration_ms is None:
            cur = conn.execute("SELECT started_at FROM dag_runs WHERE id = ?", (run_id,))
            row = cur.fetchone()
            if row:
                start_dt = parse_iso(row["started_at"])
                end_dt = parse_iso(ended)
                if start_dt and end_dt:
                    duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        conn.execute(
            "UPDATE dag_runs SET ended_at = ?, status = ?, duration_ms = ? "
            "WHERE id = ? AND status = 'running'",
            (ended, status, duration_ms, run_id),
        )
        conn.commit()


def start_stage(
    run_id: int,
    stage_name: str,
    log_path: str | None = None,
    started_at: str | None = None,
) -> int:
    """Insert a new stage row. Returns the new stage_id."""
    started = started_at or now_iso()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO dag_stages (run_id, stage_name, started_at, status, log_path) "
            "VALUES (?, ?, ?, 'running', ?)",
            (run_id, stage_name, started, log_path),
        )
        conn.commit()
        # lastrowid is non-None after a successful INSERT; coalesce for the type.
        return int(cur.lastrowid or 0)


def finish_stage(
    stage_id: int,
    status: str,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    stderr_excerpt: str | None = None,
    ended_at: str | None = None,
) -> None:
    """Mark a stage finished. ``status`` ∈ {ok, skipped, failed, cancelled}.

    Idempotent: guarded on ``status = 'running'`` for the same reason as
    :func:`finish_run` — a late trap must not flip ``ok`` rows to ``failed``.
    """
    ended = ended_at or now_iso()
    with closing(_connect()) as conn, conn:
        if duration_ms is None:
            cur = conn.execute("SELECT started_at FROM dag_stages WHERE id = ?", (stage_id,))
            row = cur.fetchone()
            if row:
                start_dt = parse_iso(row["started_at"])
                end_dt = parse_iso(ended)
                if start_dt and end_dt:
                    duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        conn.execute(
            "UPDATE dag_stages SET ended_at = ?, status = ?, duration_ms = ?, "
            "exit_code = ?, stderr_excerpt = ? "
            "WHERE id = ? AND status = 'running'",
            (
                ended,
                status,
                duration_ms,
                exit_code,
                stderr_excerpt,
                stage_id,
            ),
        )
        conn.commit()


# ── Read API ─────────────────────────────────────────────────────────────────


def _load_stages_for(conn: sqlite3.Connection, run_ids: list[int]) -> dict[int, list[DagStageRow]]:
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    cur = conn.execute(
        f"SELECT * FROM dag_stages WHERE run_id IN ({placeholders}) "
        f"ORDER BY run_id, started_at, id",
        run_ids,
    )
    out: dict[int, list[DagStageRow]] = {rid: [] for rid in run_ids}
    for row in cur.fetchall():
        out[row["run_id"]].append(_row_to_stage(row))
    return out


def get_recent_runs(limit: int = 20, engine: str | None = None) -> list[DagRunRow]:
    """Most recent runs, newest first, each with its stages attached.

    Pass ``engine`` to scope to one engine (``'ingestion'``, ``'briefing'``, or
    ``'distill'``). ``None`` returns every engine's runs
    interleaved by id."""
    with closing(_connect()) as conn, conn:
        # Order by id, not started_at: id is the monotonic insertion order,
        # whereas started_at is a text timestamp that can mix tz offsets
        # (live rows are UTC, log-backfilled rows are local) and would then
        # sort lexically wrong.
        if engine is None:
            cur = conn.execute(
                "SELECT * FROM dag_runs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM dag_runs WHERE engine = ? ORDER BY id DESC LIMIT ?",
                (engine, max(1, int(limit))),
            )
        rows = cur.fetchall()
        run_ids = [row["id"] for row in rows]
        stages_by_run = _load_stages_for(conn, run_ids)
        return [_row_to_run(row, stages_by_run.get(row["id"], [])) for row in rows]


def get_run(run_id: int) -> DagRunRow | None:
    with closing(_connect()) as conn, conn:
        cur = conn.execute("SELECT * FROM dag_runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        stages_by_run = _load_stages_for(conn, [run_id])
        return _row_to_run(row, stages_by_run.get(run_id, []))


def reconcile_orphaned_runs(pid_file_exists: bool = False, engine: str = "ingestion") -> None:
    """Mark abandoned ``running`` runs as ``cancelled`` when no live owner exists.

    ``pid_file_exists`` is a historical name for "a live engine still owns this":
    the caller now derives it from the DB-backed ``engine_lock`` (a live ingestion
    holder), not the old ``/tmp`` pid file. Without an ``engine`` scope, a server
    restart while the briefing engine is mid-run would cancel that live engine too,
    so every reconcile pass is scoped to a single engine and each engine's
    lifecycle stays independent.
    """
    if pid_file_exists:
        return
    now = now_iso()
    with closing(_connect()) as conn, conn:
        # ``cancelled`` rather than ``failed`` — an orphaned stage was killed
        # by a restart/crash, the connector itself never reported a failure.
        # The UI relies on this distinction to render a neutral pill instead
        # of a misleading red "FAILED".
        conn.execute(
            "UPDATE dag_stages SET status = 'cancelled', "
            "ended_at = COALESCE(ended_at, ?) "
            "WHERE status = 'running' "
            "AND run_id IN (SELECT id FROM dag_runs WHERE engine = ?)",
            (now, engine),
        )
        conn.execute(
            "UPDATE dag_runs SET status = 'cancelled', "
            "ended_at = COALESCE(ended_at, ?) "
            "WHERE status = 'running' AND engine = ?",
            (now, engine),
        )
        conn.commit()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_core.dag_state",
        description="Record DAG run/stage lifecycle events into SQLite.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start-run", help="Record a new run start; prints the new run_id.")
    sp.add_argument("--trigger", required=True)
    sp.add_argument("--log-path", required=True)
    sp.add_argument("--err-path", required=True)
    sp.add_argument("--started-at", default=None)
    # Without --engine the CLI would always insert engine='ingestion', so
    # briefing runs launched by a shell wrapper would never appear under their
    # own engine tab in the UI.
    sp.add_argument(
        "--engine",
        default="ingestion",
        choices=["ingestion", "briefing", "distill"],
    )

    sp = sub.add_parser("finish-run", help="Mark a run finished.")
    sp.add_argument("--run-id", type=int, required=True)
    sp.add_argument("--status", required=True, choices=["ok", "failed", "cancelled"])
    sp.add_argument("--duration-ms", type=int, default=None)
    sp.add_argument("--ended-at", default=None)

    sp = sub.add_parser("start-stage", help="Record a stage start; prints the new stage_id.")
    sp.add_argument("--run-id", type=int, required=True)
    sp.add_argument("--stage", required=True)
    sp.add_argument("--log-path", default=None)
    sp.add_argument("--started-at", default=None)

    sp = sub.add_parser("finish-stage", help="Mark a stage finished.")
    sp.add_argument("--stage-id", type=int, required=True)
    sp.add_argument(
        "--status",
        required=True,
        choices=["ok", "skipped", "failed", "cancelled"],
    )
    sp.add_argument("--exit-code", type=int, default=None)
    sp.add_argument("--duration-ms", type=int, default=None)
    sp.add_argument("--stderr-excerpt", default=None)
    sp.add_argument("--ended-at", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "start-run":
        run_id = start_run(
            args.trigger,
            args.log_path,
            args.err_path,
            args.started_at,
            engine=args.engine,
        )
        print(run_id)
    elif args.cmd == "finish-run":
        finish_run(args.run_id, args.status, args.duration_ms, args.ended_at)
    elif args.cmd == "start-stage":
        stage_id = start_stage(args.run_id, args.stage, args.log_path, args.started_at)
        print(stage_id)
    elif args.cmd == "finish-stage":
        finish_stage(
            args.stage_id,
            args.status,
            exit_code=args.exit_code,
            duration_ms=args.duration_ms,
            stderr_excerpt=args.stderr_excerpt,
            ended_at=args.ended_at,
        )
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())


__all__ = [
    "DagRunRow",
    "DagStageRow",
    "db_path",
    "finish_run",
    "finish_stage",
    "get_recent_runs",
    "get_run",
    "reconcile_orphaned_runs",
    "start_run",
    "start_stage",
]
