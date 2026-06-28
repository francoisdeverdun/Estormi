#!/usr/bin/env python3
"""Logically mark reminders that are no longer pending as completed in the DB.

Extracted verbatim from the second ``python3 - <<'PYEOF'`` heredoc that used to
live inside ``reminders/watch_and_ingest.sh`` so the logic is importable and
unit-testable.

Invoked once per run, AFTER a clean export, by the shell:

    python3 -m estormi_ingestion.reminders.mark_complete <exported_json> <db_path>

``exported_json`` is a JSON array of the reminder source_ids that are currently
pending (one per staged ``*.meta.json``); ``db_path`` is the chunk SQLite store.
Every ``reminders`` chunk whose ``source_id`` is NOT in that set is flagged
``completed = 1`` so the daily briefing stops surfacing it as overdue. The
chunks are kept for historical search.

This is a destructive UPDATE: the shell only calls it when the exporter wrote
``_export_complete.flag`` (a guarantee that EVERY reminder was enumerated), so a
partial export can never wrongly mark live reminders completed.

STDOUT contract: prints exactly one line — ``[reminders] Marked N reminder(s)
as completed`` — when at least one reminder was newly completed; otherwise
nothing. Exits 0.
"""

from __future__ import annotations

import json
import sqlite3
import sys


def main(argv: list[str]) -> int:
    exported_ids = set(json.loads(argv[1]))
    db_path = argv[2]

    conn = sqlite3.connect(db_path, timeout=10)
    c = conn.cursor()
    # Idempotent migration — column may not exist yet if the MCP server hasn't restarted.
    try:
        c.execute("ALTER TABLE chunks ADD COLUMN completed INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    c.execute("SELECT source_id, id FROM chunks WHERE source = 'reminders' AND completed = 0")
    rows = c.fetchall()

    newly_done = [row[1] for row in rows if row[0] not in exported_ids]
    if newly_done:
        c.executemany("UPDATE chunks SET completed = 1 WHERE id = ?", [(i,) for i in newly_done])
        conn.commit()
        print(f"[reminders] Marked {len(newly_done)} reminder(s) as completed")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
