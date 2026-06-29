"""Briefing-suite shared fixtures.

The briefing modules live in the ``estormi_briefing`` package
(importable via the repo root the root conftest puts on ``sys.path``), so no
per-suite path setup is needed here.
"""

from __future__ import annotations

import pytest

from tests.helpers.database import apply_runtime_schema


@pytest.fixture
async def actions_db(tmp_path):
    """Yield an aiosqlite connection with the runtime schema, for action tests."""
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "test.db"))
    db.row_factory = aiosqlite.Row
    await apply_runtime_schema(db)
    yield db
    await db.close()


@pytest.fixture
async def db_path(tmp_path):
    """Initialise a runtime-schema sqlite DB at a known path, with the standard
    knowledge settings seeded, then close it.

    Pipeline tests typically patch
    ``estormi_briefing.run_briefing.DB_PATH`` and let the real
    code path open its own connection — they just need a primed file.
    """
    import aiosqlite

    db_path = str(tmp_path / "test.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await apply_runtime_schema(conn)
    # OR REPLACE so individual tests can override these seeds without
    # tripping the UNIQUE constraint.
    await conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('knowledge_llm_provider', 'local')"
    )
    await conn.commit()
    await conn.close()
    return db_path


@pytest.fixture
def yaml_path(tmp_path):
    """A minimal one-source YAML config the run() tests usually need."""
    p = tmp_path / "sources.yaml"
    p.write_text(
        "sources:\n  - id: ch\n    label: Ch\n    type: youtube_channel\n"
        "    url: https://www.youtube.com/@Ch/videos\n    axis: tech\n"
        "    mode: news\n    subtitle_langs: [fr]\n"
    )
    return p
