#!/usr/bin/env python3
"""Chunk one staged Apple Note and POST it to the MCP server.

Extracted verbatim from the ``python3 - <<'PYEOF'`` heredoc that used to live
inside ``apple_notes/watch_and_ingest.sh`` so the logic is importable and
unit-testable — the heredoc body was never executed by the test suite, which is
exactly how a ``post_chunks`` ``TypeError`` once shipped to a 03:00 ingestion
run.

Invoked once per staged note by the shell loop:

    python3 -m estormi_ingestion.apple_notes.ingest \\
        <meta_file> <html_file> <mcp_url> <repo_root> <chunk_size>

The positional argv order is preserved exactly from the old heredoc
(``"$PY" - "$meta_file" "$html_file" "$MCP_URL" "$REPO_ROOT" "$CHUNK_SIZE"``).
``repo_root`` (argv[4]) is no longer consumed — imports now resolve through
``-m`` / ``PYTHONPATH`` — but the slot is kept so the shell's argv shape is
byte-identical.

STDOUT contract (the shell captures it into ``CHUNKS_FOR_NOTE``): on a clean run
this prints exactly one line — the number of chunks indexed (``ok``); on an
empty / OTP note it prints ``0``. The shell sums these into ``chunk_total``.
Nothing else may be written to stdout; diagnostics go to stderr.

Exit-code contract (the shell gates the watermark on a clean exit):
    0 — note ingested cleanly, was empty, or was an OTP/verification note
    1 — at least one chunk failed to POST (staged files kept for retry)
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

from estormi_ingestion.shared.chunker import paragraph_chunks
from estormi_ingestion.shared.emit import post_chunks
from memory_core.pii_filter import filter_pii, is_otp_message


def main(argv: list[str]) -> int:
    meta_path = Path(argv[1])
    html_path = Path(argv[2])
    mcp_url = argv[3].rstrip("/")
    chunk_size = int(argv[5])

    meta = json.loads(meta_path.read_text())
    raw_html = html_path.read_text(errors="ignore")
    # Map block-level tags to blank lines BEFORE stripping tags, so the note's
    # own paragraph/heading/list structure survives as \n\n breaks that
    # paragraph_chunks splits on — flattening all whitespace would fuse
    # unrelated sections of one note into a single chunk.
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|ul|ol|tr|table|blockquote)>", "\n\n", raw_html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        print(0)
        return 0

    # Skip OTP/verification notes entirely — single-source policy: a whole
    # message/note that is just a verification code has no residual value
    # once redacted. WhatsApp drops OTP lines within a window; document and
    # single-message sources bail.
    if is_otp_message(text):
        print(f"[notes]   skipped OTP/verification note {meta.get('id', '')}", file=sys.stderr)
        print(0)
        return 0

    text = filter_pii(text)

    chunks = paragraph_chunks(text, max_size=chunk_size, min_size=80)

    def _log(idx, status):
        # Surface only failures — per-chunk success is drowned out by the
        # per-note progress line printed by the shell loop above.
        if status not in ("ok", "skipped", "dry"):
            print(f"      chunk {idx}: {status}", file=sys.stderr)

    ok, _skipped, failed = post_chunks(
        "notes",
        meta.get("id", ""),
        chunks,
        mcp_url=mcp_url,
        title=meta.get("title", ""),
        date=meta.get("date", ""),
        meta={"pii_filtered": True},
        on_result=_log,
    )
    # Non-zero exit keeps staged files for the next run (the shell loop gates the
    # watermark on a clean ingest); a benign skip (duplicate) does not fail.
    if failed:
        return 1
    print(ok)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
