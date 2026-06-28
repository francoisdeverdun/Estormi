#!/usr/bin/env python3
"""Wipe Qdrant collection + truncate SQLite chunks and watermarks (forces full re-ingest).

Keeps settings only. Mirrors the in-app reset (`api/admin.py`). Invoked by
`make reset`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make `estormi_server` importable when run as a bare file: it lives under
# packages/, which isn't on sys.path for `python scripts/reset_data.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))

from estormi_server.storage.tools import COLLECTION, DATA_DIR, _client  # noqa: E402


async def _run() -> None:
    import aiosqlite

    db = await aiosqlite.connect(os.path.join(DATA_DIR, "estormi.db"))
    # Delete in dependency order, matching api/admin.py's reset.
    await db.execute("DELETE FROM ingestion_watermarks")
    await db.execute("DELETE FROM chunks")
    await db.commit()
    # Reclaim disk space — must run outside a transaction.
    await db.execute("VACUUM")
    await db.close()

    c = _client()
    cols = {x.name for x in (await c.get_collections()).collections}
    if COLLECTION in cols:
        await c.delete_collection(COLLECTION)
    print("Done — chunks + watermarks cleared, Qdrant collection dropped.")


def _confirmed() -> bool:
    """Gate the irreversible wipe behind explicit confirmation.

    The in-app reset is behind a confirm modal; `make reset` had no such
    guard, so a mistyped/tab-completed command could destroy the user's
    personal-memory archive. Mirror the GUI's care: prompt on a TTY, refuse
    when non-interactive unless explicitly authorized. Bypass for scripted
    use with ``--yes``/``-y`` or ``ESTORMI_RESET_YES=1``.
    """
    if "--yes" in sys.argv or "-y" in sys.argv or os.environ.get("ESTORMI_RESET_YES") == "1":
        return True
    if not sys.stdin.isatty():
        print(
            "reset_data: refusing to wipe the archive non-interactively — "
            "re-run with --yes (or ESTORMI_RESET_YES=1) to confirm.",
            file=sys.stderr,
        )
        return False
    print("This DELETES all chunks + watermarks and DROPS the Qdrant collection.")
    print("Your personal-memory archive is wiped (settings kept; sources re-ingestable).")
    return input("Type 'reset' to confirm: ").strip() == "reset"


if __name__ == "__main__":
    if not _confirmed():
        print("Aborted — nothing was changed.")
        sys.exit(1)
    asyncio.run(_run())
