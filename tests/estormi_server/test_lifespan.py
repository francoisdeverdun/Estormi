"""Integration tests for the FastAPI lifespan/startup sequence.

``server/lifespan.py`` is the load-bearing boot path: it applies the SQLite
schema + migrations, rewrites a stale embed-model setting, ensures the Qdrant
collection, reconciles orphaned DAG runs, and registers the scheduler jobs.
The order of operations matters — a regression here breaks first-launch crash
semantics or migration ordering and nothing else in the suite would catch it.

Strategy: drive the *real* ``lifespan`` async context manager against a
temp-file SQLite DB. External boundaries are mocked at their import site in
``server.lifespan`` / ``server.jobs`` — Qdrant (``ensure_collection``), the
legacy data-dir migration, and the scheduler. Each test asserts on observable
effects, not merely that startup ran.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration


async def _drive_lifespan(db_path: str, *, ensure_collection=None, scheduler=None):
    """Run ``lifespan`` against ``db_path`` with external seams mocked.

    Yields nothing useful itself — callers use it as an async context
    manager and inspect ``tools._db`` / the scheduler mock inside the
    ``with`` body. ``ensure_collection`` defaults to a no-op AsyncMock;
    pass an explicit mock to exercise its failure branch.
    """
    import estormi_server.server.lifespan as lifespan_mod
    from estormi_server.storage import tools

    ec = ensure_collection if ensure_collection is not None else AsyncMock()
    sched = scheduler if scheduler is not None else MagicMock()

    return _LifespanHarness(db_path, lifespan_mod, tools, ec, sched)


class _LifespanHarness:
    """Async context manager that boots ``lifespan`` with patched seams."""

    def __init__(self, db_path, lifespan_mod, tools, ensure_collection, scheduler):
        self.db_path = db_path
        self.lifespan_mod = lifespan_mod
        self.tools = tools
        self.ensure_collection = ensure_collection
        self.scheduler = scheduler
        self._patches: list = []
        self._cm = None

    async def __aenter__(self):
        data_dir = str(Path(self.db_path).parent)
        # Patch every external boundary at its import site so booting the
        # lifespan never touches the real Qdrant collection, scheduler, or
        # engine launchers.
        self._patches = [
            patch.object(self.tools, "DATA_DIR", data_dir),
            patch.object(self.tools, "DB_PATH", self.db_path),
            patch.object(self.lifespan_mod, "ensure_collection", self.ensure_collection),
            patch.object(self.lifespan_mod, "_scheduler", self.scheduler),
            # The launcher hooks lifespan registers with the scheduler (plus
            # the queue runner) — patched to no-op coroutines so the real
            # engines never fire during a test boot.
            patch.object(self.lifespan_mod, "_schedule_ingestion", AsyncMock()),
            patch.object(self.lifespan_mod, "_schedule_briefing", AsyncMock()),
            patch.object(self.lifespan_mod, "_queue_runner", AsyncMock()),
            patch.object(self.lifespan_mod, "_kill_briefing", AsyncMock()),
        ]
        for p in self._patches:
            p.start()
        self._cm = self.lifespan_mod.lifespan(MagicMock())
        await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            await self._cm.__aexit__(None, None, None)
        finally:
            for p in reversed(self._patches):
                p.stop()
        return False


# ── Schema + DB bootstrap ───────────────────────────────────────────────────


async def test_startup_applies_schema_and_opens_db(tmp_path):
    """Startup opens a live aiosqlite connection with the production schema."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        # The module-level connection is live and points at our temp DB.
        assert tools._db is not None
        cur = await tools._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
        )
        assert await cur.fetchone() is not None, "chunks table not created"
        await cur.close()
        # settings table from INIT_SQL must also exist.
        cur = await tools._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        )
        assert await cur.fetchone() is not None
        await cur.close()

    # Shutdown closes and clears the connection.
    assert tools._db is None
    assert Path(db_path).exists()


async def test_startup_sets_wal_and_foreign_keys(tmp_path):
    """The boot PRAGMAs (WAL journal, foreign keys) are applied to the live DB."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        cur = await tools._db.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        await cur.close()
        assert mode.lower() == "wal"

        cur = await tools._db.execute("PRAGMA foreign_keys")
        fk = (await cur.fetchone())[0]
        await cur.close()
        assert fk == 1


async def test_startup_chmods_db_file_to_owner_only(tmp_path):
    """The chunk DB holds personal memory in the clear, so boot tightens it to
    0o600 (owner read/write only). Asserting the real file mode after startup —
    not merely that ``os.chmod`` was called — guards the privacy invariant.
    """
    import stat

    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        mode = stat.S_IMODE(Path(db_path).stat().st_mode)
        assert mode == 0o600, f"DB file should be owner-only, got {oct(mode)}"
        # The connection is still live while we hold it.
        assert tools._db is not None


async def test_startup_creates_data_dir(tmp_path):
    """A missing data directory is created (os.makedirs exist_ok)."""

    nested = tmp_path / "fresh" / "data"
    db_path = str(nested / "estormi.db")
    assert not nested.exists()
    async with await _drive_lifespan(db_path):
        assert nested.is_dir()


async def test_startup_is_idempotent_on_rerun(tmp_path):
    """Booting twice against the same DB must not error — migrations re-run."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO chunks (id, content_hash, source) VALUES (?, ?, ?)",
            ("c1", "h1", "test"),
        )
        await tools._db.commit()

    # Second boot against the now-populated file — INIT_SQL/MIGRATION_SQL are
    # idempotent CREATE/DROP statements, so this must succeed and preserve data.
    async with await _drive_lifespan(db_path):
        cur = await tools._db.execute("SELECT COUNT(*) FROM chunks")
        count = (await cur.fetchone())[0]
        await cur.close()
        assert count == 1, "re-run must preserve existing rows"


# ── embed_model rewrite branch ──────────────────────────────────────────────


async def test_startup_rewrites_stale_lmstudio_embed_model(tmp_path):
    """A stale ``text-embedding-*`` embed_model is rewritten to the fastembed id."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    # First boot to create the schema, then seed the stale value.
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO settings (key, value) VALUES ('embed_model', ?)",
            ("text-embedding-nomic-embed-text-v1.5",),
        )
        await tools._db.commit()

    # Second boot must detect and rewrite it.
    async with await _drive_lifespan(db_path):
        cur = await tools._db.execute("SELECT value FROM settings WHERE key = 'embed_model'")
        value = (await cur.fetchone())[0]
        await cur.close()
        assert value == "nomic-ai/nomic-embed-text-v1.5"


async def test_startup_leaves_valid_embed_model_untouched(tmp_path):
    """A non-stale embed_model value is not rewritten."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO settings (key, value) VALUES ('embed_model', ?)",
            ("nomic-ai/nomic-embed-text-v1.5",),
        )
        await tools._db.commit()

    async with await _drive_lifespan(db_path):
        cur = await tools._db.execute("SELECT value FROM settings WHERE key = 'embed_model'")
        value = (await cur.fetchone())[0]
        await cur.close()
        assert value == "nomic-ai/nomic-embed-text-v1.5"


# ── Qdrant collection ───────────────────────────────────────────────────────


async def test_startup_ensures_qdrant_collection(tmp_path):
    """``ensure_collection`` is awaited exactly once during startup."""
    ec = AsyncMock()
    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path, ensure_collection=ec):
        pass
    ec.assert_awaited_once()


async def test_startup_survives_qdrant_locked(tmp_path):
    """A Qdrant failure at startup is caught — the server still comes up."""
    from estormi_server.storage import tools

    ec = AsyncMock(side_effect=RuntimeError("storage folder is already accessed"))
    db_path = str(tmp_path / "estormi.db")
    # The exception must NOT propagate out of the lifespan __aenter__.
    async with await _drive_lifespan(db_path, ensure_collection=ec):
        # DB still opened despite the Qdrant failure — startup continued.
        assert tools._db is not None
    ec.assert_awaited_once()


# ── ensure_collection: EMBED_DIM size-mismatch guard (the real coroutine) ─────
# These exercise ``qdrant_helpers.ensure_collection`` itself — only the Qdrant
# *client* is stubbed; the size-comparison logic, the actionable RuntimeError,
# and the index-creation loop all run for real. This is the guard the lifespan
# leans on (it warns-and-continues if ensure_collection raises, but the *content*
# of the error is what tells an operator how to fix a dimension drift).


class _FakeCollections:
    def __init__(self, names):
        self.collections = [type("C", (), {"name": n})() for n in names]


def _collection_info_with_dense_size(dense_name: str, size):
    """Build a stand-in for ``client.get_collection(...)`` whose dense vector
    config reports ``size`` (or omits one when ``size`` is None)."""
    dense_cfg = type("VP", (), {"size": size})()
    params = type("P", (), {"vectors": {dense_name: dense_cfg}})()
    config = type("Cfg", (), {"params": params})()
    return type("Info", (), {"config": config})()


def _fake_qdrant_client(*, existing, collection_info=None):
    client = AsyncMock()
    client.get_collections = AsyncMock(return_value=_FakeCollections(existing))
    client.create_collection = AsyncMock()
    client.delete_collection = AsyncMock()
    client.create_payload_index = AsyncMock()
    if collection_info is not None:
        client.get_collection = AsyncMock(return_value=collection_info)
    return client


async def test_ensure_collection_raises_on_dense_size_mismatch():
    """An existing collection whose dense-vector width differs from EMBED_DIM
    must raise with actionable text — a silent mismatch fails every later upsert
    with an opaque error instead."""
    from estormi_server.storage import qdrant_helpers, tools
    from memory_core.embedder import EMBED_DIM

    wrong_size = EMBED_DIM + 256
    info = _collection_info_with_dense_size(tools.DENSE_VECTOR_NAME, wrong_size)
    client = _fake_qdrant_client(existing=[tools.COLLECTION], collection_info=info)

    with patch.object(tools, "_client", return_value=client):
        with pytest.raises(RuntimeError) as exc:
            await qdrant_helpers.ensure_collection()

    msg = str(exc.value)
    assert str(wrong_size) in msg
    assert str(EMBED_DIM) in msg
    assert "admin reset" in msg  # the actionable remedy
    # The guard fired before any index work — no recreate, no new collection.
    client.create_collection.assert_not_awaited()


async def test_ensure_collection_passes_when_dense_size_matches():
    """A matching dense size clears the guard and proceeds to (idempotent)
    payload-index creation — no raise, no recreate of the existing collection."""
    from estormi_server.storage import qdrant_helpers, tools
    from memory_core.embedder import EMBED_DIM

    info = _collection_info_with_dense_size(tools.DENSE_VECTOR_NAME, EMBED_DIM)
    client = _fake_qdrant_client(existing=[tools.COLLECTION], collection_info=info)

    with patch.object(tools, "_client", return_value=client):
        await qdrant_helpers.ensure_collection()  # no raise

    client.create_collection.assert_not_awaited()  # already exists
    # The payload-index loop ran for every declared field.
    assert client.create_payload_index.await_count >= 1


async def test_ensure_collection_creates_when_absent():
    """A fresh (absent) collection is created with the dense+sparse vector
    config — the guard branch is skipped entirely when there's nothing to check."""
    from estormi_server.storage import qdrant_helpers, tools

    client = _fake_qdrant_client(existing=[])  # collection does not exist yet

    with patch.object(tools, "_client", return_value=client):
        await qdrant_helpers.ensure_collection()

    client.create_collection.assert_awaited_once()
    # The dense vector is sized to EMBED_DIM at creation.
    from memory_core.embedder import EMBED_DIM

    kwargs = client.create_collection.await_args.kwargs
    dense = kwargs["vectors_config"][tools.DENSE_VECTOR_NAME]
    assert dense.size == EMBED_DIM


# ── Scheduler job registration ──────────────────────────────────────────────


def _job_ids(scheduler_mock) -> list[str]:
    return [c.kwargs.get("id") for c in scheduler_mock.add_job.call_args_list]


async def test_startup_registers_default_scheduler_jobs(tmp_path):
    """With default settings, the cron scheduler jobs are registered + started.

    The queue runner itself is an asyncio task, not a cron.
    """
    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path, scheduler=sched):
        pass
    ids = _job_ids(sched)
    assert "daily_dag" in ids
    assert "daily_briefing" in ids
    sched.start.assert_called_once()


async def test_startup_skips_dag_job_when_schedule_manual(tmp_path):
    """``schedule_cron='manual'`` ⇒ no ``daily_dag`` job registered."""
    from estormi_server.storage import tools

    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO settings (key, value) VALUES ('schedule_cron', 'manual')"
        )
        await tools._db.commit()

    async with await _drive_lifespan(db_path, scheduler=sched):
        pass
    assert "daily_dag" not in _job_ids(sched)


async def test_startup_skips_briefing_job_when_schedule_manual(tmp_path):
    """``briefing_schedule_cron='manual'`` ⇒ no ``daily_briefing`` job."""
    from estormi_server.storage import tools

    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO settings (key, value) VALUES ('briefing_schedule_cron', 'manual')"
        )
        await tools._db.commit()

    async with await _drive_lifespan(db_path, scheduler=sched):
        pass
    assert "daily_briefing" not in _job_ids(sched)


async def test_startup_honours_custom_cron_schedule(tmp_path):
    """A custom ``schedule_cron`` is fed into ``CronTrigger.from_crontab``."""
    from estormi_server.storage import tools

    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO settings (key, value) VALUES ('schedule_cron', '0 4 * * *')"
        )
        await tools._db.commit()

    async with await _drive_lifespan(db_path, scheduler=sched):
        pass
    dag_call = next(c for c in sched.add_job.call_args_list if c.kwargs.get("id") == "daily_dag")
    # Second positional arg is the trigger built from the crontab string.
    trigger_repr = repr(dag_call.args[1])
    assert "hour='4'" in trigger_repr or "hour=4" in trigger_repr


# ── Orphaned DAG-run reconciliation ─────────────────────────────────────────


async def test_startup_reconciles_orphaned_dag_runs(tmp_path):
    """A leftover ``running`` dag_run with no live engine lock is marked cancelled.

    Both ``reconcile_orphaned_runs`` (via ``dag_state._connect()``) and the
    lifespan's liveness check (via ``engine_lock.current()``) open their own sync
    connections; point both ``DB_PATH_OVERRIDE``s at the same temp file the
    lifespan opens so everything touches one database. The lock is empty (no
    pipeline ran), so the orphaned run reconciles to cancelled.
    """
    from estormi_server.storage import tools
    from memory_core import dag_state, engine_lock

    db_path = str(tmp_path / "estormi.db")
    # Boot once to create the schema, then seed a stuck "running" row.
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO dag_runs (status, started_at) VALUES (?, ?)",
            ("running", "2026-05-01T00:00:00"),
        )
        await tools._db.commit()

    original_dag_override = dag_state.DB_PATH_OVERRIDE
    original_lock_override = engine_lock.DB_PATH_OVERRIDE
    dag_state.DB_PATH_OVERRIDE = db_path
    engine_lock.DB_PATH_OVERRIDE = db_path
    try:
        # No live lock holder ⇒ reconciliation treats the run as orphaned.
        async with await _drive_lifespan(db_path):
            cur = await tools._db.execute("SELECT status FROM dag_runs ORDER BY id DESC LIMIT 1")
            status = (await cur.fetchone())[0]
            await cur.close()
    finally:
        dag_state.DB_PATH_OVERRIDE = original_dag_override
        engine_lock.DB_PATH_OVERRIDE = original_lock_override
    assert status == "cancelled", "orphaned run should be marked cancelled"


async def test_startup_does_not_cancel_a_live_pipeline_run(tmp_path):
    """The dangerous direction: a ``running`` row whose engine lock names a LIVE
    ingestion owner must be left alone — reconciliation must NOT cancel a genuine
    in-flight shell-launched DAG just because the server restarted under it.
    """
    import subprocess
    import sys

    from estormi_server.storage import tools
    from memory_core import dag_state, engine_lock

    db_path = str(tmp_path / "estormi.db")
    async with await _drive_lifespan(db_path):
        await tools._db.execute(
            "INSERT INTO dag_runs (status, started_at) VALUES (?, ?)",
            ("running", "2026-05-01T00:00:00"),
        )
        await tools._db.commit()

    # A real, alive process in its OWN session (pid == pgid) stands in for the
    # shell-launched DAG. Using its own group — not the test's — so lifespan
    # *shutdown* (which kills the recorded pipeline group) can't terminate pytest.
    live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    original_dag_override = dag_state.DB_PATH_OVERRIDE
    original_lock_override = engine_lock.DB_PATH_OVERRIDE
    dag_state.DB_PATH_OVERRIDE = db_path
    engine_lock.DB_PATH_OVERRIDE = db_path
    try:
        engine_lock.acquire("ingestion", live.pid, live.pid)
        async with await _drive_lifespan(db_path):
            cur = await tools._db.execute("SELECT status FROM dag_runs ORDER BY id DESC LIMIT 1")
            status = (await cur.fetchone())[0]
            await cur.close()
        engine_lock.force_release()
    finally:
        live.kill()
        live.wait()
        dag_state.DB_PATH_OVERRIDE = original_dag_override
        engine_lock.DB_PATH_OVERRIDE = original_lock_override
    assert status == "running", "a live pipeline's run must not be cancelled on restart"


async def test_startup_survives_reconcile_failure(tmp_path):
    """A failure inside ``reconcile_orphaned_runs`` is caught — startup continues."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    with patch(
        "memory_core.dag_state.reconcile_orphaned_runs",
        side_effect=RuntimeError("db is locked"),
    ):
        async with await _drive_lifespan(db_path):
            # Startup reached the DB-open step despite the reconcile failure.
            assert tools._db is not None


# ── Shutdown teardown ───────────────────────────────────────────────────────


async def test_shutdown_stops_scheduler_and_kills_engines(tmp_path):
    """Exiting the lifespan stops the scheduler and kills the briefing engine.

    The harness installs an ``AsyncMock`` teardown stub for ``_kill_briefing``;
    this test reaches into it to assert it was awaited exactly once during
    ``__aexit__``.
    """
    from estormi_server.storage import tools

    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")

    harness = await _drive_lifespan(db_path, scheduler=sched)
    await harness.__aenter__()
    # Grab the patched-in teardown stub before __aexit__ stops the patches.
    kill_brief = harness.lifespan_mod._kill_briefing
    await harness.__aexit__(None, None, None)

    assert tools._db is None
    sched.shutdown.assert_called_once_with(wait=False)
    kill_brief.assert_awaited_once()


async def test_shutdown_kills_dag_process_group(tmp_path):
    """Shutdown routes through ``_kill_dag_processes`` so the same SIGTERM →
    SIGKILL escalation applies as during runtime kills. Simulates the
    well-behaved case: SIGTERM lands and the next signal-0 probe finds the
    group gone.
    """
    from estormi_server.server import jobs as jobs_mod

    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")
    original_pgid = jobs_mod._dag_pgid
    import signal as _signal

    sigterm_seen = False
    signals_sent: list[int] = []

    def _killpg(pgid, sig):
        nonlocal sigterm_seen
        signals_sent.append(sig)
        if sig == _signal.SIGTERM:
            sigterm_seen = True
            return
        if sig == 0:
            # The DAG exited cleanly after SIGTERM — the probe reports gone.
            raise ProcessLookupError
        raise AssertionError(f"unexpected signal {sig}")

    with patch.object(jobs_mod.os, "killpg", side_effect=_killpg):
        try:
            async with await _drive_lifespan(db_path, scheduler=sched):
                jobs_mod._dag_pgid = 999999
        finally:
            jobs_mod._dag_pgid = original_pgid

    assert sigterm_seen, "shutdown must send SIGTERM via _kill_dag_processes"
    assert _signal.SIGKILL not in signals_sent, "clean exit — no SIGKILL"


async def test_shutdown_killpg_swallows_process_lookup_error(tmp_path):
    """A stale PGID (process already gone on SIGTERM) must not crash shutdown."""
    from estormi_server.server import jobs as jobs_mod

    sched = MagicMock()
    db_path = str(tmp_path / "estormi.db")
    original_pgid = jobs_mod._dag_pgid

    with patch.object(jobs_mod.os, "killpg", side_effect=ProcessLookupError) as mock_killpg:
        try:
            async with await _drive_lifespan(db_path, scheduler=sched):
                jobs_mod._dag_pgid = 424242
            assert mock_killpg.called
        finally:
            jobs_mod._dag_pgid = original_pgid


async def test_shutdown_closes_db_connection(tmp_path):
    """After shutdown the module-level DB handle is closed and nulled."""
    from estormi_server.storage import tools

    db_path = str(tmp_path / "estormi.db")
    captured = {}
    async with await _drive_lifespan(db_path):
        captured["conn"] = tools._db
    assert tools._db is None
    # The captured connection must be closed — using it raises.
    with pytest.raises(Exception):
        await captured["conn"].execute("SELECT 1")


# ── Legacy data-dir migration branch ────────────────────────────────────────


# ── startup catch-up staleness (sweep 3 D3) ───────────────────────────────────


class TestIngestionCatchupStale:
    """Bug D3: the startup catch-up computed staleness from ``last.started_at``
    alone, so a run that *started* recently but was ``cancelled`` (the
    crash-recovery case ``reconcile_orphaned_runs`` produces) looked fresh and
    the catch-up was skipped — exactly the run that needs re-ingesting.

    These cases are pure logic, but the module is marked ``integration``;
    pytest *merges* class- and module-level ``pytestmark`` rather than
    overriding, so a class-level ``unit`` marker here would make every case run
    in both ``make test-unit`` and ``make test-integration``. We keep the
    module marker and let them run as integration only."""

    @staticmethod
    def _run(status: str, started_at):
        from types import SimpleNamespace  # noqa: PLC0415

        return SimpleNamespace(status=status, started_at=started_at)

    def test_no_prior_run_is_stale(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        import estormi_server.server.lifespan as lifespan_mod  # noqa: PLC0415

        now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        assert lifespan_mod._ingestion_catchup_stale(None, now) is True

    def test_recent_cancelled_run_is_stale(self):
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        import estormi_server.server.lifespan as lifespan_mod  # noqa: PLC0415

        now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        # The fix: a recently-started-but-crashed run must trigger catch-up.
        last = self._run("cancelled", now - timedelta(hours=1))
        assert lifespan_mod._ingestion_catchup_stale(last, now) is True

    def test_recent_ok_run_is_not_stale(self):
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        import estormi_server.server.lifespan as lifespan_mod  # noqa: PLC0415

        now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        last = self._run("ok", now - timedelta(hours=1))
        assert lifespan_mod._ingestion_catchup_stale(last, now) is False

    def test_old_ok_run_is_stale(self):
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        import estormi_server.server.lifespan as lifespan_mod  # noqa: PLC0415

        now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        last = self._run("ok", now - timedelta(hours=25))
        assert lifespan_mod._ingestion_catchup_stale(last, now) is True
