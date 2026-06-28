"""DB-backed advisory engine lock — the single "one engine at a time" guard.

Estormi runs three long-lived engines (the ingestion pipeline, briefing
composition, and the optional distillation retrain) that share heavy resources
(the local LLM, Qdrant, SQLite), so the
product contract is that only one runs at a time. The FastAPI server serialises
its *own* launches through an in-process asyncio queue, but the ingestion
pipeline can ALSO start out-of-process (``scripts/daily_ingestion.sh`` via
``make daily-dag`` or launchd). This module is the one place both the server and
the shell script agree on. It replaces the old ``/tmp/estormi-dag-<port>.pid``
file (and its hand-rolled bash mirror) and the pattern-based ``pkill -f`` /
``pgrep -f`` liveness probes — a single, ownership-checked source of truth.

The lock is a singleton row in the live SQLite DB (the same file the chunk store
and :mod:`memory_core.dag_state` use). ``acquire`` runs inside a
``BEGIN IMMEDIATE`` transaction so two racing acquirers can never both win —
SQLite permits exactly one writer. A held lock whose owning process is confirmed
dead (a signal-0 probe on the same host) is *stolen*, so an engine killed with
SIGKILL — which skips any cleanup — never wedges the slot. Cross-host ownership
is never stolen and an uncertain probe assumes alive, so a genuinely-running
engine is never healed away.

Public API:
    acquire(kind, pid, pgid=None, source=None, host=None) -> "acquired" | "held"
    release(kind, pid) -> bool
    current() -> EngineLockRow | None
    is_alive(row) -> bool
    force_release() -> bool
    ensure_schema(conn)            # async, for the server's aiosqlite startup

CLI (used by scripts/daily_ingestion.sh):
    python -m memory_core.engine_lock acquire --kind ingestion --pid $$ --pgid $$ --source schedule
        → exit 0 if acquired, 1 if held by a live engine (caller refuses to start)
    python -m memory_core.engine_lock release --kind ingestion --pid $$
    python -m memory_core.engine_lock status
"""

from __future__ import annotations

import argparse
import os
import platform
import sqlite3
import sys
from dataclasses import dataclass

import structlog

from .settings import DB_PATH
from .timeparse import now_iso

log = structlog.get_logger(__name__)

# ── Schema ───────────────────────────────────────────────────────────────────
# Owned solely by this module (like dag_state's tables). A singleton row: the
# CHECK pins id=1 so a second INSERT can only ever conflict, never add a 2nd row.
ENGINE_LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    kind        TEXT NOT NULL,
    pid         INTEGER NOT NULL,
    pgid        INTEGER,
    host        TEXT NOT NULL,
    source      TEXT,
    acquired_at TEXT NOT NULL
);
"""


async def ensure_schema(conn) -> None:
    """Apply the engine_lock DDL to an ``aiosqlite.Connection``.

    Idempotent. Called from server startup (``server.lifespan``) and the test
    schema helper so the table exists before the first acquire; the sync CLI
    path self-applies as well (see :func:`_connect`) for shell-first runs.
    """
    await conn.executescript(ENGINE_LOCK_SCHEMA)
    await conn.commit()


# ── Connection helper ────────────────────────────────────────────────────────

# Tests monkeypatch this to point at a tmp DB (mirrors dag_state.DB_PATH_OVERRIDE).
DB_PATH_OVERRIDE: str | None = None


def db_path() -> str:
    """Resolve the live Estormi DB path — same precedence as dag_state.db_path."""
    return DB_PATH_OVERRIDE or os.getenv("ESTORMI_DB_PATH", DB_PATH)


_SCHEMA_APPLIED_PATHS: set[str] = set()


def _connect() -> sqlite3.Connection:
    """Open a synchronous sqlite3 connection in autocommit mode.

    ``isolation_level=None`` puts the connection in autocommit so the explicit
    ``BEGIN IMMEDIATE`` in :func:`acquire` / :func:`release` controls the
    transaction directly (Python's implicit-BEGIN would otherwise defer the
    write lock and reopen the same race the IMMEDIATE close). WAL lets this sync
    writer coexist with the server's async aiosqlite connection on the same file.
    """
    path = db_path()
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if path not in _SCHEMA_APPLIED_PATHS:
        conn.executescript(ENGINE_LOCK_SCHEMA)
        _SCHEMA_APPLIED_PATHS.add(path)
    return conn


# ── Row type ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineLockRow:
    kind: str
    pid: int
    pgid: int | None
    host: str
    source: str | None
    acquired_at: str


def _this_host() -> str:
    return platform.node() or "unknown"


def _read(conn: sqlite3.Connection) -> EngineLockRow | None:
    r = conn.execute(
        "SELECT kind, pid, pgid, host, source, acquired_at FROM engine_lock WHERE id = 1"
    ).fetchone()
    if r is None:
        return None
    return EngineLockRow(
        kind=r["kind"],
        pid=r["pid"],
        pgid=r["pgid"],
        host=r["host"],
        source=r["source"],
        acquired_at=r["acquired_at"],
    )


# ── Liveness ─────────────────────────────────────────────────────────────────


def is_alive(row: EngineLockRow | None) -> bool:
    """Best-effort: does the engine that owns ``row`` still have a live process?

    Never steals across hosts (a remote pid is unknowable here) and errs toward
    *alive* on any ambiguity, so a genuinely-running engine is never healed away
    — only a confirmed-dead one frees the slot. Probes the process group first,
    then the bare pid, so a process that isn't its own group leader (a manual
    ``make daily-dag`` shell) isn't misread as dead just because ``killpg``
    can't see a group.
    """
    if row is None:
        return False
    if row.host != _this_host():
        return True
    for probe, target in ((os.killpg, row.pgid), (os.kill, row.pid)):
        if target is None:
            continue
        try:
            probe(target, 0)  # signal 0 — existence check, delivers nothing
            return True
        except ProcessLookupError:
            continue  # this probe says gone; try the other before declaring dead
        except PermissionError:
            return True  # exists but owned by another uid — assume alive
        except OSError:
            return True  # uncertain — never heal a maybe-live engine
    return False


# ── Public API ───────────────────────────────────────────────────────────────


def acquire(
    kind: str,
    pid: int,
    pgid: int | None = None,
    source: str | None = None,
    host: str | None = None,
) -> str:
    """Take the engine slot for ``kind``. Returns ``"acquired"`` or ``"held"``.

    A live holder (any kind — the lock is the cross-engine mutex) yields
    ``"held"``. A holder whose process is confirmed dead is stolen and replaced.
    The whole check-then-take runs under ``BEGIN IMMEDIATE`` so two racers can't
    both acquire.
    """
    host = host or _this_host()
    pgid = pid if pgid is None else pgid
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _read(conn)
        if existing is not None:
            if is_alive(existing):
                conn.execute("ROLLBACK")
                return "held"
            conn.execute("DELETE FROM engine_lock WHERE id = 1")  # dead owner — steal
        conn.execute(
            "INSERT INTO engine_lock (id, kind, pid, pgid, host, source, acquired_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?)",
            (kind, int(pid), int(pgid), host, source, now_iso()),
        )
        conn.execute("COMMIT")
        return "acquired"
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def release(kind: str, pid: int) -> bool:
    """Release the slot iff it is held by exactly ``(kind, pid)``. Returns True if freed.

    Scoping the DELETE to the caller's own ``(kind, pid)`` means a late release
    from a preempted run can't clobber the lock a newer run already took.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM engine_lock WHERE id = 1 AND kind = ? AND pid = ?", (kind, int(pid))
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def current() -> EngineLockRow | None:
    """The current lock holder, or ``None`` when the slot is free."""
    conn = _connect()
    try:
        return _read(conn)
    finally:
        conn.close()


def force_release() -> bool:
    """Drop the lock unconditionally (manual break-glass). Returns True if a row was removed.

    Deliberately not wired into any shipping caller — it is a hand-invoked admin
    escape hatch (``python -c 'from memory_core.engine_lock import force_release;
    force_release()'``) to clear a wedged lock. Unlike :func:`release` this is NOT
    owner-scoped — it can drop a live engine's slot and let a second engine start,
    so every actual drop is logged with the evicted holder for after-the-fact
    forensics.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        evicted = _read(conn)
        cur = conn.execute("DELETE FROM engine_lock WHERE id = 1")
        conn.execute("COMMIT")
        if evicted is not None:
            log.warning(
                "engine_lock.force_released",
                kind=evicted.kind,
                pid=evicted.pid,
                host=evicted.host,
                acquired_at=evicted.acquired_at,
            )
        return cur.rowcount > 0
    finally:
        conn.close()


# ── CLI (scripts/daily_ingestion.sh) ─────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m memory_core.engine_lock")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_acq = sub.add_parser("acquire", help="take the engine slot (exit 1 if held)")
    p_acq.add_argument("--kind", required=True)
    p_acq.add_argument("--pid", type=int, required=True)
    p_acq.add_argument("--pgid", type=int, default=None)
    p_acq.add_argument("--source", default=None)

    p_rel = sub.add_parser("release", help="release the slot if owned by (kind, pid)")
    p_rel.add_argument("--kind", required=True)
    p_rel.add_argument("--pid", type=int, required=True)

    sub.add_parser("status", help="print the current holder")

    args = parser.parse_args(argv)
    if args.cmd == "acquire":
        result = acquire(args.kind, args.pid, args.pgid, args.source)
        print(result)
        return 0 if result == "acquired" else 1
    if args.cmd == "release":
        print("released" if release(args.kind, args.pid) else "noop")
        return 0
    if args.cmd == "status":
        row = current()
        print("idle" if row is None else f"{row.kind} pid={row.pid} alive={is_alive(row)}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_main())
