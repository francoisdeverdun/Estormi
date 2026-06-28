"""Shared chunk-emit loop for the file/text connectors.

Several connectors (documents, world-knowledge) do the
same thing once they have a source's full text: chunk it, hash the text once,
POST each chunk to ``/ingest_chunk`` with a ``content_hash`` of ``f"{base}-{idx}"``,
and tally the per-chunk ``ok`` / ``skipped`` / ``failed`` outcomes.
:func:`post_chunks` centralises that loop; per-source differences (title, extra
payload fields, the hash base, dry-run) stay as parameters so each caller keeps
its exact payload shape.

Kept dependency-light (only ``estormi_ingestion.shared.http_client``) so it works inside the
bundled standalone Python the shell connectors pipe into.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from estormi_ingestion.shared import http_client


def content_base_hash(source_id: str, text: str) -> str:
    """The content_hash base every producer MUST use: ``sha256(f"{source_id}|{text}")``.

    ``source_id`` has to be folded in because the server dedups GLOBALLY on
    ``content_hash`` (writers.ingest_chunk), so two distinct files/events with
    byte-identical text would otherwise collide and the second be silently
    dropped. This is the single home for that rule — every connector (and
    post_chunks' default) routes through it so the convention can't drift again.
    """
    return hashlib.sha256(f"{source_id}|{text}".encode()).hexdigest()


@dataclass
class EmitCounts:
    """Per-source emit tally. Unpacks as ``ok, skipped, failed``."""

    ok: int = 0
    skipped: int = 0
    failed: int = 0

    def __iter__(self):
        yield self.ok
        yield self.skipped
        yield self.failed


def post_chunks(
    source: str,
    source_id: str,
    chunks: Iterable[str],
    *,
    mcp_url: str,
    title: str,
    date: str | None = None,
    url: str | None = None,
    chat_id_raw: str | None = None,
    meta: dict[str, Any] | None = None,
    base_hash: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
    dry_run: bool = False,
    on_result: Callable[[int, str], None] | None = None,
) -> EmitCounts:
    """POST each chunk to ``{mcp_url}/ingest_chunk`` and tally the outcomes.

    Builds the canonical payload — ``text`` / ``source`` / ``source_id`` /
    ``title`` / ``content_hash`` (``f"{base}-{idx}"``) / ``meta`` plus optional
    ``date``, ``url`` and ``chat_id_raw`` — for every chunk, so callers no longer
    hand-roll the enumerate→payload→post loop. ``chat_id_raw`` is a *top-level*
    ingest field (not a ``meta`` key — see ``estormi_server/api/ingest.py``): it
    is the per-conversation id (iMessage chat GUID, mail-thread key, WhatsApp
    JID) the server stores in the ``chat_id_raw`` column so same-conversation
    chunks group together during ``fetch_around`` retrieval. ``base_hash``
    defaults to ``sha256`` of
    ``source_id|<concatenated chunks>`` — the id MUST be folded in because the
    server's dedup is GLOBAL on ``content_hash`` (writers.py), so two distinct
    files/sources with byte-identical text would otherwise collide and all but
    one be silently dropped. Pass an explicit value only for a different hashing
    scheme (the world connector already folds the id in the same way).

    The server's reply ``status`` is mapped to ``ok`` / ``skipped`` (anything
    non-ok, e.g. a duplicate) and any exception to ``failed``. ``on_result`` is
    invoked per chunk with ``(idx, status_label)`` for callers that print
    progress.
    """
    chunk_list = list(chunks)
    if base_hash is None:
        base_hash = content_base_hash(source_id, "".join(chunk_list))
    counts = EmitCounts()
    for idx, chunk in enumerate(chunk_list):
        payload: dict[str, Any] = {
            "text": chunk,
            "source": source,
            "source_id": source_id,
            "title": title,
            "content_hash": f"{base_hash}-{idx}",
            "meta": meta if meta is not None else {},
        }
        if date is not None:
            payload["date"] = date
        if url is not None:
            payload["url"] = url
        if chat_id_raw is not None:
            payload["chat_id_raw"] = chat_id_raw

        if dry_run:
            counts.ok += 1
            if on_result is not None:
                on_result(idx, "dry")
            continue

        try:
            r = http_client.post_chunk(
                f"{mcp_url}/ingest_chunk", payload, headers=headers, timeout=timeout
            )
            body = r.json()
            status = body.get("status")
            if status == "ok":
                counts.ok += 1
            elif status == "error":
                # Server rejected the chunk (e.g. a validation failure) — that's
                # a failure, not a benign skip like a duplicate.
                counts.failed += 1
            else:
                counts.skipped += 1
            if on_result is not None:
                on_result(idx, status or body.get("reason", "?"))
        except Exception as exc:
            counts.failed += 1
            if on_result is not None:
                on_result(idx, f"ERROR {exc}")
    return counts
