"""Performance benchmark: cold-start cost of real Estormi initialisation.

Benchmarks applying the production SQLite schema (``INIT_SQL`` +
``MIGRATION_SQL`` from ``estormi_server/sql/schema.py``) to a fresh database.

A regression that bloats the schema with an expensive migration would show
up here. (The previous ``test_fastapi_app_import_latency`` benchmark was
removed: by the time this module ran, ``main`` was already imported by the
conftest setup chain, so the test timed a no-op cache hit. ``importlib.reload``
would destabilise the shared FastAPI app.)
"""

from __future__ import annotations

import sqlite3
import time

import pytest

pytestmark = pytest.mark.performance

# Generous ceiling for shared CI runners.
MAX_SCHEMA_MS = 1500


def test_runtime_schema_apply_latency():
    """Applying the real production schema to a fresh DB stays fast and correct."""
    from estormi_server.sql.schema import INIT_SQL, MIGRATION_SQL

    conn = sqlite3.connect(":memory:")
    try:
        start = time.perf_counter()
        conn.executescript(INIT_SQL)
        conn.executescript(MIGRATION_SQL)
        conn.commit()
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Correctness of the benchmarked operation: the core runtime tables
        # must exist once the real schema has been applied.
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        assert {"chunks", "settings"} <= names
    finally:
        conn.close()

    assert elapsed_ms < MAX_SCHEMA_MS, (
        f"Applying the runtime schema took {elapsed_ms:.1f} ms (limit {MAX_SCHEMA_MS} ms)"
    )
