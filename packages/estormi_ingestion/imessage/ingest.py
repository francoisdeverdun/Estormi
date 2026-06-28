#!/usr/bin/env python3
"""Chunk one staged iMessage and POST it to the MCP server.

Extracted verbatim from the ``python3 - <<'PYEOF'`` heredoc that used to live
inside ``imessage/watch_and_ingest.sh`` so the logic is importable and
unit-testable — the heredoc body was never executed by the test suite, which is
exactly how a ``post_chunks`` ``TypeError`` once shipped to a 03:00 ingestion
run.

Invoked once per staged message by the shell loop:

    python3 -m estormi_ingestion.imessage.ingest \\
        <meta_file> <body_file> <mcp_url> <repo_root> <chunk_size> <chunk_overlap>

The positional argv order is preserved exactly from the old heredoc
(``"$PY" - "$meta_file" "$body_file" "$MCP_URL" "$REPO_ROOT" "$CHUNK_SIZE"
"$CHUNK_OVERLAP"``). ``repo_root`` (argv[4]) is no longer consumed — imports now
resolve through ``-m`` / ``PYTHONPATH`` — but the slot is kept so the shell's
argv shape is byte-identical.

Exit-code contract (the shell gates the watermark on a clean exit):
    0 — message ingested cleanly, was empty, or was an OTP/2FA message
    1 — at least one chunk failed to POST (staged files kept for retry)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from estormi_ingestion.shared.chunker import paragraph_chunks, sliding_chunks
from estormi_ingestion.shared.emit import post_chunks
from memory_core.pii_filter import filter_pii, is_otp_message


def main(argv: list[str]) -> int:
    meta_path = Path(argv[1])
    body_path = Path(argv[2])
    mcp_url = argv[3].rstrip("/")
    chunk_size = int(argv[5])
    chunk_overlap = int(argv[6])

    meta = json.loads(meta_path.read_text())
    text = body_path.read_text(errors="ignore")
    # One staged file = one message body. Preserve its internal paragraph
    # structure (a long message can carry its own line breaks) so a structured
    # message splits on its own boundaries instead of a fixed window — collapse
    # only intra-line whitespace, not newlines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return 0

    # Drop OTP / 2FA messages whole — same policy as Apple Notes. A single
    # iMessage that is just a verification code has no residual value once
    # the code itself is redacted, so don't keep the surrounding boilerplate
    # either ("Your code is …", "do not share …"). WhatsApp drops OTP lines
    # *within* a multi-message window because the rest of the chat still has
    # value; single-message sources just bail.
    if is_otp_message(text):
        return 0

    text = filter_pii(text)

    msg_id = meta.get("id", "")
    name = meta.get("name", "") or meta.get("from", "unknown")
    chat_name = meta.get("chat_name", "") or meta.get("chat_id", "")
    date = meta.get("timestamp_iso", "")
    chat_id = meta.get("chat_id", "")

    title = f"iMessage — {chat_name}" if chat_name else f"iMessage — {name}"
    # Hash on chat_id:msg_id:text so the content_hash is stable per message and
    # distinct across chats — keep this base rather than emit.py's default
    # sha256(text), which would collide identical short messages across chats.
    base = hashlib.sha256(f"{chat_id}:{msg_id}:{text}".encode()).hexdigest()

    # Structure-aware chunking when the message carries its own paragraph
    # structure (split on its boundaries instead of a fixed window, the
    # cross-source fusion root cause flagged in chunker.py). A mono-block message
    # (the common case — most are one line) has no structure to respect, so window
    # it; this also gives a long single-line message overlapping windows that
    # paragraph_chunks would not. min_size=1 keeps even terse one-word replies.
    if "\n" in text:
        chunks = paragraph_chunks(text, max_size=chunk_size, min_size=1)
    else:
        chunks = sliding_chunks(text, size=chunk_size, overlap=chunk_overlap)

    def _log(idx, status):
        if status not in ("ok", "skipped", "dry"):
            sys.stderr.write(f"      chunk {idx}: {status}\n")

    _ok, _skipped, failed = post_chunks(
        "imessage",
        msg_id,
        chunks,
        mcp_url=mcp_url,
        title=title,
        date=date,
        meta={"pii_filtered": True},
        chat_id_raw=chat_id,
        base_hash=base,
        on_result=_log,
    )
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
