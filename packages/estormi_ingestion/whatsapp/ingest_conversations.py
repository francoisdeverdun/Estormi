#!/usr/bin/env python3
"""Conversation-aware WhatsApp ingestion.

Reads staged message files, groups them into conversation windows
(WHATSAPP_WINDOW_GAP_SECONDS of silence = new conversation), and ingests
each window as message-based sliding sub-windows formatted as:

    [Name]: message text
    [Other]: reply text
    ...

Group type (group/dm/broadcast) is derived automatically from the JID suffix
stored in each staged message's chat_id. The whatsapp_chats table in the DB
is populated automatically on every ingestion run — no manual config needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from estormi_ingestion.shared.config import mcp_url
from estormi_ingestion.shared.http_client import post_batch
from estormi_ingestion.shared.paths import estormi_db_path
from memory_core.pii_filter import filter_pii, is_otp_message
from memory_core.timeparse import parse_iso

STAGING = Path(os.environ.get("STAGING_DIR", Path.home() / "estormi-staging" / "whatsapp"))
MCP_URL = mcp_url()

_group_type_cache: dict[str, str] = {}
# chat_id → resolved human chat name, sourced from /api/whatsapp/chats. The
# server resolves macOS-Contacts names once at chat-list retrieval and persists
# them into whatsapp_chats.chat_name, so the name a DM staged as a bare number
# is recovered here without this script touching the address book itself.
_chat_name_cache: dict[str, str] = {}
WINDOW_GAP = int(os.environ.get("WHATSAPP_WINDOW_GAP_SECONDS", "1800"))  # 30 min
MIN_TEXT_LEN = int(os.environ.get("WHATSAPP_MIN_TEXT_LEN", "20"))

# Message-level sliding window for chunking.
MSG_CHUNK_MSGS = int(os.environ.get("WHATSAPP_MSG_CHUNK_MSGS", "12"))
MSG_CHUNK_STEP = int(os.environ.get("WHATSAPP_MSG_CHUNK_STEP", "6"))

# Durable WhatsApp message log. The bridge's offline-queue drain can only ever
# deliver a *new* message once — WhatsApp never re-sends an acked message and
# there is no "fetch since timestamp" in the protocol — so the log is the local
# source of truth. Staged messages are appended here (raw text), then `whatsapp`
# chunks are derived from it by a timestamp watermark, which makes re-ingestion
# (re-chunk / re-embed) possible without ever re-contacting WhatsApp. Bounded by
# a retention sweep.
LOG_WATERMARK_SOURCE = "whatsapp_log"
LOG_RETENTION_DAYS = int(os.environ.get("WHATSAPP_LOG_RETENTION_DAYS", "90"))

# Matches messages that carry no real text: only whitespace, emoji, emoji
# modifiers and common punctuation. The character class enumerates the
# intended Unicode emoji blocks explicitly — the previous form mixed bare
# emoji codepoints into range expressions (e.g. `\U0001f937‍♀-♂`), which
# silently created unintended descending/oversized ranges.
_TRIVIAL_RE = re.compile(
    "^["
    r"\s"  # whitespace
    ".,!?;:\\-_'\"()\\[\\]"  # common punctuation
    "‍"  # zero-width joiner (emoji sequences)
    "︎️"  # variation selectors (text/emoji)
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "\U0001f300-\U0001f5ff"  # misc symbols & pictographs
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f700-\U0001f77f"  # alchemical symbols
    "\U0001f900-\U0001f9ff"  # supplemental symbols & pictographs
    "\U0001fa70-\U0001faff"  # symbols & pictographs extended-A
    "☀-⛿"  # miscellaneous symbols
    "✀-➿"  # dingbats
    "⬀-⯿"  # miscellaneous symbols & arrows
    "\U0001f000-\U0001f0ff"  # mahjong/domino/playing-card symbols
    "]+$",
    flags=re.UNICODE,
)


def _is_trivial(text: str) -> bool:
    cleaned = _TRIVIAL_RE.sub("", text).strip()
    return len(cleaned) < MIN_TEXT_LEN


def _parse_ts(ts_iso: str) -> float:
    # parse_iso handles a trailing 'Z' and treats a naive timestamp as UTC —
    # bare fromisoformat would choke on 'Z' and read naive as *local* time.
    dt = parse_iso(ts_iso)
    return dt.timestamp() if dt else 0.0


def _load_chat_meta() -> tuple[dict[str, str], dict[str, str]]:
    """Fetch ``(group_type, chat_name)`` maps for all known chats from the server.

    Hits ``/api/whatsapp/chats`` — which resolves + persists macOS-Contacts
    names for phone-number DMs — and returns one map of ``chat_id → group_type``
    and one of ``chat_id → resolved chat_name``. Retries on transient
    connection/5xx errors: a single un-retried GET against a slow MCP startup
    or a 502 used to silently tag every chunk ``group_type=unknown`` and leave
    DM names unresolved.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.get(f"{MCP_URL}/api/whatsapp/chats", timeout=10)
            r.raise_for_status()
            data = r.json()
            group_types = {c["chat_id"]: c["group_type"] for c in data}
            names = {
                c["chat_id"]: c["chat_name"] for c in data if (c.get("chat_name") or "").strip()
            }
            return group_types, names
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
    print(
        f"[whatsapp] Could not fetch chat metadata after 3 attempts: {last_exc}",
        file=sys.stderr,
    )
    return {}, {}


def _group_type_for(chat_id: str) -> str:
    return _group_type_cache.get(chat_id, "unknown")


def _resolved_chat_name(chat_id: str) -> str:
    """Server-resolved human name for ``chat_id``, or "".

    Sourced from ``/api/whatsapp/chats``, which is the single owner of name
    resolution: the server matches a phone-number DM against the macOS address
    book there and persists the result. Ingestion never touches the Contacts
    index itself — it only reads the names the server already resolved, so the
    apps→mcp-server layering holds and there is one canonical place names come
    from. A DM the server hasn't named yet falls back to the staged name / JID.
    """
    return _chat_name_cache.get(chat_id, "")


def _resolve_unknown_names(chat_ids: set[str]) -> None:
    """Resolve names for chats we still have no name for, into the cache.

    ``/api/whatsapp/chats`` only resolves chats already present as rows, which
    the sidecar's chat-list enrichment populates on its own cycle. A brand-new
    DM's first messages can therefore be chunked under a raw JID before the row
    exists — even when the contact is in the macOS address book. This asks the
    server to resolve the missing ids directly against Contacts (and persist
    the result), closing that race. Best-effort: a failure just leaves the
    names unresolved, and the post-run back-fill heals them next time.
    """
    pending = sorted(cid for cid in chat_ids if cid and not _resolved_chat_name(cid))
    if not pending:
        return
    try:
        r = httpx.post(
            f"{MCP_URL}/api/whatsapp/resolve-names",
            json={"chat_ids": pending},
            headers={"X-Estormi-Origin": "tauri"},
            timeout=15,
        )
        r.raise_for_status()
        resolved = {cid: nm for cid, nm in (r.json() or {}).items() if nm}
    except Exception as exc:
        print(f"[whatsapp] resolve-names failed: {exc}", file=sys.stderr)
        return
    if resolved:
        _chat_name_cache.update(resolved)
        print(f"[whatsapp] Resolved {len(resolved)} previously-unnamed chat(s).")


# ── Staged message loading ────────────────────────────────────────────────────


def load_staged() -> list[dict]:
    """Read all staged (meta + body) file pairs from STAGING."""
    messages = []
    for meta_file in sorted(STAGING.glob("*.meta.json")):
        stem = meta_file.name[: -len(".meta.json")]
        body_file = STAGING / (stem + ".txt")
        if not body_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            text = body_file.read_text(errors="ignore").strip()
            if not text:
                continue
            messages.append(
                {
                    "meta_file": meta_file,
                    "body_file": body_file,
                    "text": text,
                    "msg_id": meta.get("id", ""),
                    "chat_id": meta.get("chat_id", "unknown"),
                    "chat_name": meta.get("chat_name", ""),
                    "name": meta.get("name", "unknown"),
                    "timestamp_iso": meta.get("timestamp_iso", "1970-01-01T00:00:00+00:00"),
                }
            )
        except Exception as exc:
            print(f"[whatsapp] Error reading {meta_file.name}: {exc}", file=sys.stderr)
    return messages


# ── Conversation windowing ────────────────────────────────────────────────────


def group_into_windows(messages: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Return list of (chat_id, chat_name, window_messages) sorted by time."""
    by_chat: dict[str, list[dict]] = {}
    chat_names: dict[str, str] = {}
    for msg in messages:
        cid = msg["chat_id"]
        by_chat.setdefault(cid, []).append(msg)
        if msg["chat_name"] and cid not in chat_names:
            chat_names[cid] = msg["chat_name"]

    windows: list[tuple[str, str, list[dict]]] = []
    for chat_id, msgs in by_chat.items():
        msgs.sort(key=lambda m: _parse_ts(m["timestamp_iso"]))
        chat_name = chat_names.get(chat_id, "")
        current: list[dict] = []
        for msg in msgs:
            ts = _parse_ts(msg["timestamp_iso"])
            if current and ts - _parse_ts(current[-1]["timestamp_iso"]) > WINDOW_GAP:
                windows.append((chat_id, chat_name, current))
                current = []
            current.append(msg)
        if current:
            windows.append((chat_id, chat_name, current))

    windows.sort(key=lambda w: _parse_ts(w[2][0]["timestamp_iso"]))
    return windows


# ── Message-based chunking ───────────────────────────────────────────────────


def message_sub_windows(window: list[dict], chunk_msgs: int, step: int) -> list[list[dict]]:
    """Slide a window of chunk_msgs messages over the conversation, step messages at a time."""
    if len(window) <= chunk_msgs:
        return [window]
    sub_windows: list[list[dict]] = []
    for i in range(0, len(window), step):
        chunk = window[i : i + chunk_msgs]
        if chunk:
            sub_windows.append(chunk)
        # Once a chunk reaches the end of the window, the slide is complete.
        # Continuing would emit a tail that is wholly contained in the previous
        # chunk's overlap region — pure duplicate work for the embedder/LLM.
        if i + chunk_msgs >= len(window):
            break
    return sub_windows


_OPAQUE_SENDER_RE = re.compile(r"^\+?\d[\d\s().-]{4,}$")
# A WhatsApp @mention in message text — the addressee's JID user-part, e.g.
# ``@33612345678`` (phone) or ``@100000000000002`` (a 15-digit @lid handle).
_MENTION_RE = re.compile(r"@(\d{6,})\b")


def _name_for_handle(handle: str) -> str:
    """Resolve a sender/mention handle to a known contact name, or "".

    Accepts a bare number ("33612345678"), a full JID ("…@s.whatsapp.net" /
    "…@lid") or a JID user-part, and looks it up against the server-resolved
    chat-name cache — which already carries macOS-Contacts names for phone DMs
    and push_names for @lid DMs. A group-member handle the user has no other
    chat with stays unresolved: WhatsApp transmits no name for it.
    """
    h = (handle or "").strip()
    if not h:
        return ""
    if h in _chat_name_cache:
        return _chat_name_cache[h]
    digits = re.sub(r"\D", "", h.split("@", 1)[0])
    if not digits:
        return ""
    for jid in (f"{digits}@s.whatsapp.net", f"{digits}@lid"):
        nm = _chat_name_cache.get(jid)
        if nm:
            return nm
    return ""


def _display_name(name: str, chat_id: str) -> str:
    """Best human label for a message sender.

    WhatsApp stages a sender as ``Me``, a ``push_name``, or — when neither is
    known — a bare phone number / raw ``@lid`` handle. For the bare-number case
    in a 1:1 chat, the only other party is the chat partner, so the
    server-resolved chat name (macOS Contacts / push_name, persisted at
    chat-list retrieval) is exactly who it is. For a group member staged as a
    raw handle, fall back to any name we know for that handle from another chat.
    """
    s = (name or "").strip()
    # Opaque = a bare phone number, or the connector's "[unknown]" placeholder
    # for a sender whose push_name wasn't captured. In a 1:1 DM the other party
    # is the chat partner, so resolve to the chat's server-resolved name.
    is_opaque = s.lower() == "unknown" or (
        bool(_OPAQUE_SENDER_RE.match(s)) and not any(c.isalpha() for c in s)
    )
    if is_opaque and chat_id.endswith("@s.whatsapp.net"):
        resolved = _resolved_chat_name(chat_id)
        if resolved:
            return resolved
    # A sender staged as a raw handle (``<num>@lid`` group member, bare number)
    # may still be someone we know from another chat — resolve it if so.
    if "@" in s or (s and not any(c.isalpha() for c in s)):
        known = _name_for_handle(s)
        if known:
            return known
    return name


def _resolve_mentions(text: str) -> str:
    """Rewrite ``@<number>`` WhatsApp mentions to ``@<name>`` when known.

    Besides restoring who a message addresses, this strips the bare digit run
    that the PII filter would otherwise mistake for an NIR / card number — a
    15-digit ``@lid`` mention used to be redacted as SOCIAL_SECURITY, erasing
    the addressee. Unknown mentions collapse to ``@…`` so no raw number leaks.
    """

    def repl(match: re.Match[str]) -> str:
        name = _name_for_handle(match.group(1))
        return f"@{name}" if name else "@…"

    return _MENTION_RE.sub(repl, text)


def _format_sub_window(sub_window: list[dict]) -> str:
    """Render the sub-window as redacted text. Drops OTP-only messages."""
    lines = []
    for msg in sub_window:
        raw = re.sub(r"\s+", " ", msg["text"]).strip()
        # An SMS/WhatsApp message that is just an OTP must not be indexed —
        # filter_pii leaves a [REDACTED:OTP_CODE] marker long enough to slip
        # past _is_trivial, so drop the whole line instead.
        if is_otp_message(raw):
            continue
        raw = _resolve_mentions(raw)
        lines.append(f"[{_display_name(msg['name'], msg['chat_id'])}]: {filter_pii(raw)}")
    return "\n".join(lines)


def _format_sub_window_raw(sub_window: list[dict]) -> str:
    """Same as _format_sub_window but without PII redaction.

    Used for triviality checks so the long [REDACTED:...] markers don't
    inflate a message's length past MIN_TEXT_LEN.
    """
    lines = []
    for msg in sub_window:
        raw = re.sub(r"\s+", " ", msg["text"]).strip()
        if is_otp_message(raw):
            continue
        lines.append(f"[{_display_name(msg['name'], msg['chat_id'])}]: {raw}")
    return "\n".join(lines)


# ── Ingestion ─────────────────────────────────────────────────────────────────


# ingest_window outcomes. A window's staged files may only be deleted when
# the outcome is OK or TRIVIAL — FAILED means a chunk POST errored (e.g. a
# transient MCP outage) and the files must be kept for a later retry.
OUTCOME_OK = "ok"
OUTCOME_TRIVIAL = "trivial"
OUTCOME_FAILED = "failed"


def ingest_window(
    chat_id: str,
    chat_name: str,
    window: list[dict],
) -> str:
    """Ingest one conversation window as message-based sub-window chunks.

    Returns one of OUTCOME_OK / OUTCOME_TRIVIAL / OUTCOME_FAILED. Only OK and
    TRIVIAL windows are safe to unlink; FAILED windows must be retried.
    """
    # Triviality check uses the raw (un-redacted) text — see comment on
    # _format_sub_window_raw for why filter_pii markers must not count.
    if _is_trivial(_format_sub_window_raw(window)):
        return OUTCOME_TRIVIAL

    first_ts = window[0]["timestamp_iso"]
    # Prefer the server-resolved chat name (macOS Contacts / push_name,
    # persisted at chat-list retrieval) over the staged one; then the staged
    # name, then the JID.
    resolved_title = _resolved_chat_name(chat_id) or chat_name or chat_id
    title = f"WhatsApp — {resolved_title}"
    window_id = f"{chat_id}:{first_ts}"
    # The base hash must reflect the window's *current* message set, not just
    # (chat_id, first_ts). A conversation window grows across runs when new
    # messages arrive within WINDOW_GAP of the last one: group_into_windows
    # folds them into the same window with an unchanged first_ts. If the base
    # hash keyed only on (chat_id, first_ts), idx0's content_hash would be
    # identical run-to-run, so ingest_chunk's duplicate short-circuit would skip
    # it before the source_id stale-replace path could run — the appended
    # messages would never be indexed. Folding the last message's timestamp and
    # the message count in makes a grown window yield a fresh base_hash, so the
    # superseded idx chunks are retired via the existing source_id path.
    last_ts = window[-1]["timestamp_iso"]
    base_hash = hashlib.sha256(f"{window_id}:{last_ts}:{len(window)}".encode()).hexdigest()

    sub_windows = message_sub_windows(window, MSG_CHUNK_MSGS, MSG_CHUNK_STEP)
    payloads: list[dict] = []

    for idx, sub_win in enumerate(sub_windows):
        if _is_trivial(_format_sub_window_raw(sub_win)):
            continue
        chunk_text = _format_sub_window(sub_win)
        if not chunk_text.strip():
            continue

        payloads.append(
            {
                "text": chunk_text,
                "source": "whatsapp",
                "source_id": window_id,
                "title": title,
                "date": first_ts,
                "content_hash": f"{base_hash}-{idx}",
                "chat_id_raw": chat_id,
                "chat_name": (resolved_title if resolved_title not in (chat_id, "") else chat_name)
                or resolved_title,
                "group_type": _group_type_for(chat_id),
                "meta": {"pii_filtered": True},
            }
        )

    if not payloads:
        return OUTCOME_OK

    any_failed = False
    batch_size = 50
    for i in range(0, len(payloads), batch_size):
        batch = payloads[i : i + batch_size]
        try:
            r = post_batch(f"{MCP_URL}/ingest_batch", batch, timeout=120)
            r.raise_for_status()
            print(f"  [{title[:45]}] batch {i // batch_size}: {len(batch)} chunks OK")
        except Exception as exc:
            any_failed = True
            print(
                f"  [{title[:45]}] batch {i // batch_size}: ERROR {exc}",
                file=sys.stderr,
            )

    if any_failed:
        return OUTCOME_FAILED
    return OUTCOME_OK


# ── Durable message log (replayable source of truth) ─────────────────────────


def _log_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(estormi_db_path(), timeout=30)
    # Match the watermark helper's pragmas so a log write waits out a long ingest
    # write instead of erroring with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _msg_log_id(msg: dict) -> str:
    """Stable primary key for the log. Prefer the WhatsApp message id; fall back
    to a content hash so an id-less staged message still dedups idempotently."""
    mid = (msg.get("msg_id") or "").strip()
    if mid:
        return mid
    basis = f"{msg.get('chat_id', '')}|{msg.get('timestamp_iso', '')}|{msg.get('text', '')}"
    return "h:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _utc_iso(ts_iso: str) -> str:
    """Canonical UTC ``+00:00`` isoformat for a log timestamp.

    Ordering contract: every ``ts_iso`` stored in ``whatsapp_messages`` (and the
    watermark derived from it) is a uniform UTC isoformat string, so the SQL
    ``ts_iso >= ?`` range scan and the Python ``max()`` watermark — both plain
    string comparisons — order by real instant. Producers already emit UTC (the
    Rust bridge's ``to_rfc3339(Utc)``, fetch_imessages' ``isoformat()``), but
    nothing upstream enforces it, so normalise at the single append point. An
    unparseable timestamp collapses to the epoch, matching ``load_staged``'s
    fallback.
    """
    dt = parse_iso(ts_iso)
    if dt is None:
        return "1970-01-01T00:00:00+00:00"
    return dt.astimezone(timezone.utc).isoformat()


def append_to_log(messages: list[dict]) -> int:
    """Append staged messages to ``whatsapp_messages`` (idempotent by msg_id).

    Returns the number of *new* rows inserted. Raw text is stored verbatim — PII
    redaction happens downstream at chunk time, so the log can replay re-chunking
    / re-embedding without re-fetching from WhatsApp. Timestamps are normalised
    to canonical UTC (see ``_utc_iso``) so string comparisons on ``ts_iso``
    order by instant.
    """
    if not messages:
        return 0
    rows = [
        (
            _msg_log_id(m),
            m.get("chat_id", "unknown"),
            m.get("chat_name", ""),
            m.get("name", ""),
            _utc_iso(m.get("timestamp_iso", "")),
            m.get("text", ""),
        )
        for m in messages
    ]
    conn = _log_conn()
    try:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO whatsapp_messages "
            "(msg_id, chat_id, chat_name, sender_name, ts_iso, text) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def _row_to_msg(row: tuple) -> dict:
    """Shape a ``whatsapp_messages`` row like ``load_staged`` output so
    ``group_into_windows`` can consume it directly."""
    return {
        "msg_id": row[0],
        "chat_id": row[1],
        "chat_name": row[2] or "",
        "name": row[3] or "",
        "timestamp_iso": row[4],
        "text": row[5],
        # Log-sourced messages aren't backed by staging files.
        "meta_file": None,
        "body_file": None,
    }


def load_log_since(cutoff_iso: str) -> list[dict]:
    """Load log messages with ``ts_iso >= cutoff_iso`` (empty = all).

    The string comparison is sound because ``append_to_log`` normalises every
    stored ``ts_iso`` to canonical UTC (see ``_utc_iso``).
    """
    conn = _log_conn()
    try:
        cur = conn.execute(
            "SELECT msg_id, chat_id, chat_name, sender_name, ts_iso, text "
            "FROM whatsapp_messages WHERE ts_iso >= ? ORDER BY ts_iso",
            (cutoff_iso,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_row_to_msg(r) for r in rows]


def _extend_to_window_starts(msgs: list[dict]) -> list[dict]:
    """Walk each chat in the slice back to the true start of its conversation.

    The replay cutoff is global (watermark − 2×WINDOW_GAP across *all* chats),
    so it can fall in the middle of a conversation — one longer than the
    overlap, or one in a chat that went quiet while another stayed active.
    Re-windowing the truncated slice would derive a different first message,
    hence a different ``window_id`` (= ``source_id``) than the run that first
    ingested the window: the stale-replace path would never retire the old
    chunks and the conversation tail would be re-ingested as permanent
    duplicates. Prepending every earlier message still within WINDOW_GAP of
    the slice keeps ``first_ts`` — and thus ``window_id`` — stable across runs.
    """
    if not msgs:
        return msgs
    earliest: dict[str, str] = {}
    for m in msgs:
        cid = m["chat_id"]
        if cid not in earliest or m["timestamp_iso"] < earliest[cid]:
            earliest[cid] = m["timestamp_iso"]
    prepended: list[dict] = []
    conn = _log_conn()
    try:
        for chat_id, first_iso in sorted(earliest.items()):
            boundary = _parse_ts(first_iso)
            cur = conn.execute(
                "SELECT msg_id, chat_id, chat_name, sender_name, ts_iso, text "
                "FROM whatsapp_messages WHERE chat_id = ? AND ts_iso < ? "
                "ORDER BY ts_iso DESC",
                (chat_id, first_iso),
            )
            for row in cur:
                ts = _parse_ts(row[4])
                if boundary - ts > WINDOW_GAP:
                    break  # a real conversation gap — the window starts here
                prepended.append(_row_to_msg(row))
                boundary = ts
            cur.close()
    finally:
        conn.close()
    # Order doesn't matter: group_into_windows re-sorts per chat.
    return prepended + msgs


def get_log_watermark() -> str | None:
    conn = _log_conn()
    try:
        cur = conn.execute(
            "SELECT last_fetched_at FROM ingestion_watermarks WHERE source = ?",
            (LOG_WATERMARK_SOURCE,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_log_watermark(ts_iso: str) -> None:
    conn = _log_conn()
    try:
        conn.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at, last_item_id) "
            "VALUES (?, ?, NULL) "
            "ON CONFLICT(source) DO UPDATE SET last_fetched_at = excluded.last_fetched_at",
            (LOG_WATERMARK_SOURCE, ts_iso),
        )
        conn.commit()
    finally:
        conn.close()


def prune_log(retention_days: int = LOG_RETENTION_DAYS) -> int:
    """Drop log messages older than the retention horizon. Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn = _log_conn()
    try:
        before = conn.total_changes
        conn.execute("DELETE FROM whatsapp_messages WHERE ts_iso < ?", (cutoff,))
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def _window_cutoff(watermark: str | None) -> str:
    """Re-window a recent slice each run: from ``watermark`` minus an overlap of
    two window gaps (``_extend_to_window_starts`` then pulls each chat back to
    its true conversation start, so a window straddling the cutoff reconstructs
    identically). An unchanged window re-posts the same content_hash (a no-op
    skip); a window that grew gets a fresh content-derived base_hash so the
    appended messages are indexed and the superseded chunks retired.
    Empty string on the first run = window the whole log."""
    anchor = parse_iso(watermark) if watermark else None
    if anchor is None:
        return ""
    return (anchor - timedelta(seconds=WINDOW_GAP * 2)).isoformat()


def main(dry_run: bool = False) -> None:
    group_types, chat_names = _load_chat_meta()
    _group_type_cache.update(group_types)
    _chat_name_cache.update(chat_names)

    # 1. Stage → durable log. This append is the durability point: once a message
    #    is in the log, the staging file can go (the log, not staging, is the
    #    retry source). If the append itself fails, keep staging for next run.
    staged = load_staged()
    if staged:
        print(f"[whatsapp] Loaded {len(staged)} staged messages.")
        if dry_run:
            print("[whatsapp] (dry-run) skipping durable-log append + staging cleanup.")
        else:
            try:
                added = append_to_log(staged)
            except sqlite3.Error as exc:
                print(
                    f"[whatsapp] Durable-log append FAILED ({exc}); keeping staging for retry.",
                    file=sys.stderr,
                )
                return
            print(
                f"[whatsapp] Appended {added} new message(s) to durable log "
                f"({len(staged) - added} already present)."
            )
            for m in staged:
                if m.get("meta_file"):
                    Path(m["meta_file"]).unlink(missing_ok=True)
                if m.get("body_file"):
                    Path(m["body_file"]).unlink(missing_ok=True)
            print(f"[whatsapp] Cleaned up {len(staged)} staged file pair(s).")
    else:
        print("[whatsapp] No staged messages found.")

    # 2. Derive chunks from the durable log by timestamp watermark. Extend each
    #    chat back to its conversation start so a window straddling the cutoff
    #    keeps a stable window_id (see _extend_to_window_starts).
    cutoff = _window_cutoff(get_log_watermark())
    log_msgs = _extend_to_window_starts(load_log_since(cutoff))
    if not log_msgs:
        print("[whatsapp] Nothing in the durable log to (re)ingest.")
        if not dry_run:
            prune_log()
        return

    # Fill in names for any chat in this slice the chat-list pass hasn't named
    # yet (a fresh chat whose row didn't exist when its first messages were
    # staged), so chunk titles + senders carry the real name from the start.
    if not dry_run:
        _resolve_unknown_names({m["chat_id"] for m in log_msgs})

    windows = group_into_windows(log_msgs)
    print(
        f"[whatsapp] Re-windowing {len(log_msgs)} log message(s) since "
        f"{cutoff or 'the beginning'} → {len(windows)} window(s) across "
        f"{len({w[0] for w in windows})} chat(s)."
    )

    ingested = skipped = failed = 0
    for chat_id, chat_name, window in windows:
        label = chat_name or chat_id
        if dry_run:
            # Triviality check uses the raw (un-redacted) text so [REDACTED:*]
            # markers can't inflate length past MIN_TEXT_LEN.
            trivial = _is_trivial(_format_sub_window_raw(window))
            sub_wins = message_sub_windows(window, MSG_CHUNK_MSGS, MSG_CHUNK_STEP)
            print(
                f"  [{'SKIP' if trivial else 'KEEP'}] "
                f"{label[:40]} ({len(window)} msgs → {len(sub_wins)} chunks)"
            )
            if trivial:
                skipped += 1
            else:
                ingested += 1
        else:
            outcome = ingest_window(chat_id, chat_name, window)
            if outcome == OUTCOME_FAILED:
                failed += 1
            elif outcome == OUTCOME_TRIVIAL:
                skipped += 1
            else:
                ingested += 1

    suffix = f", {failed} failed" if failed else ""
    print(
        f"[whatsapp] {'(dry-run) ' if dry_run else ''}"
        f"Ingested {ingested} window(s), skipped {skipped} trivial{suffix}."
    )

    if not dry_run:
        # Advance the watermark only on a fully clean run. Any POST failure leaves
        # it put, so the next run re-windows the same slice and content_hash dedup
        # retries only what didn't land — no message is skipped on a transient
        # error, because the log still holds it.
        if failed == 0:
            set_log_watermark(max(m["timestamp_iso"] for m in log_msgs))
        else:
            print(f"[whatsapp] {failed} window(s) failed — watermark held for retry.")

        pruned = prune_log()
        if pruned:
            print(f"[whatsapp] Pruned {pruned} log message(s) older than {LOG_RETENTION_DAYS}d.")

        # Fire-and-forget the LLM auto-tag pass for any chats still flagged
        # ``unknown``. Only useful once at least one window made it into the
        # store — otherwise the sampler has nothing to feed the model and the
        # run is a no-op. The endpoint returns immediately; the worker runs
        # in-process on the server side.
        if ingested:
            try:
                httpx.post(
                    f"{MCP_URL}/api/whatsapp/chats/auto-tag",
                    json={"only_unknown": True},
                    headers={"X-Estormi-Origin": "tauri"},
                    timeout=5,
                )
            except Exception as exc:
                print(f"[whatsapp] Auto-tag trigger failed: {exc}", file=sys.stderr)

        # Heal chunks whose title is still a raw JID because the chat's name
        # resolved only after they were first ingested (a brand-new chat's
        # metadata races behind ingestion). Re-titles them in place once a name
        # is available — runs every clean pass, a no-op when nothing is opaque.
        try:
            r = httpx.post(
                f"{MCP_URL}/api/whatsapp/backfill-titles",
                headers={"X-Estormi-Origin": "tauri"},
                timeout=20,
            )
            r.raise_for_status()
            updated = (r.json() or {}).get("updated", 0)
            if updated:
                print(f"[whatsapp] Back-filled {updated} chunk title(s) with resolved names.")
        except Exception as exc:
            print(f"[whatsapp] backfill-titles failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest staged WhatsApp messages as conversation windows"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
