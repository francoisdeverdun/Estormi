#!/usr/bin/env python3
"""POST one staged reminder to the MCP server.

Extracted verbatim from the first ``python3 - <<'PYEOF'`` heredoc that used to
live inside ``reminders/watch_and_ingest.sh`` so the logic is importable and
unit-testable — the heredoc body was never executed by the test suite, which is
exactly how a ``post_chunks`` ``TypeError`` once shipped to a 03:00 ingestion
run.

Invoked once per staged reminder by the shell loop:

    python3 -m estormi_ingestion.reminders.ingest \\
        <meta_file> <body_file> <mcp_url> <repo_root>

The positional argv order is preserved exactly from the old heredoc
(``"$PY" - "$meta_file" "$body_file" "$MCP_URL" "$REPO_ROOT"``). ``repo_root``
(argv[4]) is no longer consumed — imports now resolve through ``-m`` /
``PYTHONPATH`` — but the slot is kept so the shell's argv shape is byte-identical.

A reminder is a single short record, so it is posted whole (one chunk), not
chunked. Exit-code contract (the shell gates the watermark on a clean exit):
    0 — reminder ingested cleanly, was empty, or was an OTP/verification reminder
    1 — the POST failed (staged files kept for retry)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from estormi_ingestion.shared.emit import post_chunks
from memory_core.pii_filter import filter_pii, is_otp_message


def main(argv: list[str]) -> int:
    meta_path = Path(argv[1])
    body_path = Path(argv[2])
    mcp_url = argv[3].rstrip("/")

    meta = json.loads(meta_path.read_text())
    text = body_path.read_text(errors="ignore")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return 0

    # OTP/verification reminders have no residual memory value.
    if is_otp_message(text):
        return 0

    text = filter_pii(text)

    def _log(idx, status):
        if status not in ("ok", "skipped", "dry"):
            sys.stderr.write(f"      {status}\n")

    _ok, _skipped, failed = post_chunks(
        "reminders",
        meta.get("id", ""),
        [text],
        mcp_url=mcp_url,
        title=meta.get("title", ""),
        date=meta.get("date", ""),
        meta={"pii_filtered": True},
        on_result=_log,
    )
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
