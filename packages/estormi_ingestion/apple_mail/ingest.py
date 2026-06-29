#!/usr/bin/env python3
"""Chunk one staged Apple Mail message and POST it to the MCP server.

Extracted verbatim from the ``python3 - <<'PYEOF'`` heredoc that used to live
inside ``apple_mail/watch_and_ingest.sh`` so the logic (and especially
``thread_root_key``) is importable and unit-testable — the heredoc body was
never executed by the test suite, which is exactly how a ``post_chunks``
``TypeError`` once shipped to a 03:00 ingestion run.

Invoked once per staged message by the shell loop:

    python3 -m estormi_ingestion.apple_mail.ingest \\
        <meta_file> <body_file> <mcp_url> <repo_root> <chunk_size> <chunk_overlap>

The positional argv order is preserved exactly from the old heredoc
(``"$PY" - "$meta_file" "$body_file" "$MCP_URL" "$REPO_ROOT" "$CHUNK_SIZE"
"$CHUNK_OVERLAP"``). ``repo_root`` (argv[4]) is no longer consumed — imports now
resolve through ``-m`` / ``PYTHONPATH`` — but the slot is kept so the shell's
argv shape is byte-identical.

Exit-code contract (the shell gates the watermark on a clean exit):
    0 — message ingested cleanly, was empty, or was an OTP/verification message
    1 — at least one chunk failed to POST (staged files kept for retry)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from estormi_ingestion.shared.chunker import paragraph_chunks, sliding_chunks
from estormi_ingestion.shared.emit import content_base_hash, post_chunks
from memory_core.pii_filter import filter_pii, is_otp_message


def thread_root_key(raw_headers: str, source_id: str) -> str:
    """Derive a stable thread-root key from raw RFC822 headers.

    Picks the FIRST message-id in References:, else the id in In-Reply-To:,
    else this message's own Message-ID:, else falls back to source_id. The
    chosen id is stripped of angle brackets and hashed to a short stable key.
    Header parsing is case-insensitive and tolerant of missing/garbled
    headers — any failure simply falls back to source_id and never raises.
    """
    try:
        fields = {}
        for line in (raw_headers or "").splitlines():
            if ":" not in line or line[:1] in (" ", "\t"):
                continue  # skip continuation/garbled lines
            name, _, value = line.partition(":")
            fields.setdefault(name.strip().lower(), value.strip())
        ids = re.findall(r"<[^<>]+>", fields.get("references", ""))
        chosen = ids[0] if ids else None
        if chosen is None:
            ids = re.findall(r"<[^<>]+>", fields.get("in-reply-to", ""))
            chosen = ids[0] if ids else None
        if chosen is None:
            ids = re.findall(r"<[^<>]+>", fields.get("message-id", ""))
            chosen = ids[0] if ids else None
        if chosen is None:
            return source_id
        cleaned = chosen.strip().lstrip("<").rstrip(">").strip()
        if not cleaned:
            return source_id
        return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    except Exception:
        return source_id


def main(argv: list[str]) -> int:
    meta_path = Path(argv[1])
    body_path = Path(argv[2])
    mcp_url = argv[3].rstrip("/")
    chunk_size = int(argv[5])
    chunk_overlap = int(argv[6])

    meta = json.loads(meta_path.read_text())
    text = body_path.read_text(errors="ignore")
    # Keep the body's paragraph structure for the structure-aware chunker (a mail
    # body is prose with its own paragraph breaks) — collapse only intra-line
    # whitespace, not newlines. The OTP probe runs on a fully-flattened copy so a
    # code split across lines is still detected.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return 0

    # Skip OTP/verification mail entirely — no residual value, and the redacted
    # stub would just pollute the index.
    if is_otp_message(re.sub(r"\s+", " ", text)):
        print(f"[mail]   skipped OTP/verification message {meta.get('id', '')}", file=sys.stderr)
        return 0

    # Note: PII filter is applied AFTER prepending the header below — the
    # sender/subject can leak the same data (a phone in the subject, an email
    # in the From), so filter the concatenated text once at the end.

    title = meta.get("title", "")
    date = meta.get("date", "")
    sender = meta.get("from", "")
    source_id = meta.get("id", "")
    raw_headers = meta.get("headers", "")

    chat_id_raw = thread_root_key(raw_headers, source_id)
    header = f"From: {sender}\nSubject: {title}\n\n" if sender or title else ""
    full = filter_pii(header + text)
    base = content_base_hash(source_id, full)

    # Structure-aware chunking: split on the mail's own paragraph boundaries so a
    # chunk does not straddle two unrelated sections (the cross-source fusion root
    # cause flagged in chunker.py). Fall back to the fixed window only for a
    # mono-block body with no paragraph structure to respect.
    if "\n\n" in full:
        chunks = paragraph_chunks(full, max_size=chunk_size, min_size=1)
    else:
        chunks = sliding_chunks(full, size=chunk_size, overlap=chunk_overlap)

    def _log(idx, status):
        # Only surface failures — success is implied by the per-message line above.
        if status not in ("ok", "skipped", "dry"):
            sys.stderr.write(f"      chunk {idx}: {status}\n")

    _ok, _skipped, failed = post_chunks(
        "mail",
        source_id,
        chunks,
        mcp_url=mcp_url,
        title=title,
        date=date,
        meta={"pii_filtered": True},
        chat_id_raw=chat_id_raw,
        base_hash=base,
        on_result=_log,
    )
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
