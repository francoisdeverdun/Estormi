"""Settings lookup against the shared SQLite connection.

The shared connection itself (``_db``) and the synchronous accessor
(``sqlite_conn()``) live in :mod:`tools` because the lifespan and the test
suite mutate ``tools._db`` directly. Helpers that only *read* through
``sqlite_conn()`` can live here — :func:`_get_setting` is the first.
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)


async def _get_setting(key: str, default: str = "", *, env_override: bool = True) -> str:
    """Read a setting. Env var override → settings table → default.

    Pass ``env_override=False`` to read a key the app *writes back* as mutable
    runtime state (e.g. ``whoop_polling_last_fired_date``). Such keys must come
    from the settings table only — letting an ``ESTORMI_<KEY>`` env var shadow
    them would freeze the state and wedge whatever logic reads it back.
    """
    # Late-binding through ``tools`` so the test suite's ``tools._db = ...``
    # swap is honoured even after this helper was split into its own module.
    from estormi_server.storage import tools  # noqa: PLC0415

    if env_override:
        env_val = os.environ.get(f"ESTORMI_{key.upper()}", "")
        if env_val:
            return env_val
    try:
        db = tools.sqlite_conn()
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return row["value"] if row else default
    except Exception:
        log.warning("get_setting.lookup_failed", key=key, exc_info=True)
        return default
