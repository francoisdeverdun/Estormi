#!/usr/bin/env python3
"""iMessage ingestion — reads a snapshot of ~/Library/Messages/chat.db.

The bundled Python sidecar never inherits the app's Full Disk Access (macOS
treats the re-signed interpreter as its own TCC responsible process, which is
absent from the FDA list — so it stays denied even after the user grants the
app). Only the main app binary is covered, so it copies chat.db into the data
dir on request via the loopback API; this script reads that copy. When the host
is unreachable (a dev run from a terminal that itself holds FDA), it falls back
to reading the live original directly.

Usage:
    python3 fetch_imessages.py [--dry-run] [--days N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from estormi_ingestion.shared.paths import estormi_data_dir
from memory_core.pii_filter import is_otp_message

STAGING = Path(os.environ.get("STAGING_DIR", Path.home() / "estormi-staging/imessage"))
# First-run window when no watermark and no historic-depth choice exists.
# 90d matches the Manage modal default depth pill — a bare run
# must not silently fetch a century of history.
DAYS = int(os.environ.get("IMESSAGE_DAYS_WINDOW", "90"))

# iMessage epoch: 2001-01-01 00:00:00 UTC.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# The `message.date` column mixes two units: modern iMessage rows store
# nanoseconds since APPLE_EPOCH (~7e17 for 2024), legacy SMS/old rows store
# seconds (~7e8). Any value above this threshold is nanoseconds (it would be
# year ~2317 if read as seconds, i.e. impossible). Used by both the unit
# normaliser and the SQL cutoff so the two can never drift.
APPLE_NS_THRESHOLD = 10_000_000_000

# Exit code reserved for "Full Disk Access not granted" — distinct from the
# generic exit 1 so the orchestrator can surface it as a setup gap rather
# than a crash. Picked to match the gcal "needs re-auth" code at the same
# semantic level: 2 = "user action required, not a code bug".
EXIT_NEEDS_FDA = 2


def apple_ts_to_dt(ts: int) -> datetime:
    """Convert Apple timestamp (seconds or nanoseconds since 2001-01-01) to UTC datetime.

    Modern iMessage stores nanoseconds (~7×10¹⁷ for 2024).
    Legacy SMS/old messages store seconds (~7×10⁸ for 2024).
    Threshold: any value > 1e10 is nanoseconds (year 2317 in seconds = impossible).
    """
    try:
        if not ts:
            # A 0/NULL Apple timestamp means "unknown" — anchor to the Apple
            # epoch instead of `datetime.now()` so re-runs are deterministic
            # and timestamp-bucketed downstream code doesn't drift on retries.
            return APPLE_EPOCH
        seconds = ts / 1_000_000_000 if ts > APPLE_NS_THRESHOLD else ts
        return datetime.fromtimestamp(APPLE_EPOCH.timestamp() + seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return APPLE_EPOCH


def decode_attributed_body(blob: bytes | None) -> str:
    """Extract message text from a Messages ``attributedBody`` blob.

    Since macOS 11 the body is an archived ``NSAttributedString`` stored in
    ``message.attributedBody`` and ``message.text`` is left NULL — so reading
    only ``text`` misses almost every modern message. The text is a
    length-prefixed UTF-8 run that follows the first ``NSString`` class marker
    in the typedstream; the length uses typedstream's variable-width int
    encoding (0x81/0x82/0x83 = 2/4/8 little-endian bytes follow).
    """
    if not blob:
        return ""
    marker = blob.find(b"NSString")
    if marker == -1:
        return ""
    plus = blob.find(b"\x2b", marker + len(b"NSString"), marker + len(b"NSString") + 16)
    if plus == -1 or plus + 1 >= len(blob):
        return ""
    q = plus + 1
    n = blob[q]
    if n == 0x81:
        length = int.from_bytes(blob[q + 1 : q + 3], "little")
        q += 3
    elif n == 0x82:
        length = int.from_bytes(blob[q + 1 : q + 5], "little")
        q += 5
    elif n == 0x83:
        length = int.from_bytes(blob[q + 1 : q + 9], "little")
        q += 9
    else:
        length = n
        q += 1
    # Guard against a malformed typedstream whose declared length runs past
    # the blob — a bad length byte would otherwise silently return a
    # truncated/empty string and we'd lose the message body.
    if length < 0 or q + length > len(blob):
        return ""
    return blob[q : q + length].decode("utf-8", errors="replace")


def _safe_id(guid: str) -> str:
    return hashlib.sha256(guid.encode()).hexdigest()[:32]


def _request_snapshot() -> bool:
    """Ask the FDA-covered Tauri host to refresh the chat.db snapshot.

    The bundled Python sidecar cannot read the FDA-protected original, so the
    main app binary copies it into the data dir on request (loopback API on
    :9877, shared-token auth). Returns True when a fresh snapshot is available,
    False when the host is unreachable — e.g. a dev run from a terminal that
    itself holds FDA, which then reads the original directly.
    """
    token = os.environ.get("ESTORMI_WA_TOKEN", "")
    if not token:
        return False
    import urllib.request  # noqa: PLC0415 — stdlib, only the bundle path needs it

    req = urllib.request.Request(
        "http://127.0.0.1:9877/api/imessage/snapshot",
        method="POST",
        headers={"x-estormi-wa-token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()).get("status") == "authorized"
    except Exception:
        return False


def _resolve_chat_db() -> Path:
    """Pick the chat.db to read: explicit override → snapshot copy → the live
    original (dev fallback).

    The loopback refresh is best-effort: it can fail transiently (e.g. an
    orphaned sidecar from a prior launch holds the API port with a stale token).
    When it does, we still prefer an *existing* copy — seeded by the Rust host
    at launch — over the FDA-protected original the sidecar can't read. Only
    when no copy exists at all (a dev run with no host) do we read the original
    directly, which works there because the terminal itself holds FDA.
    """
    override = os.environ.get("IMESSAGE_DB")
    if override:
        return Path(override)
    _request_snapshot()  # best-effort refresh; the copy below is used regardless
    copy = estormi_data_dir() / "imessage" / "chat.db"
    if copy.exists():
        return copy
    return Path.home() / "Library/Messages/chat.db"


def fetch(days: int = DAYS, dry_run: bool = False) -> int:
    chat_db = _resolve_chat_db()
    if not chat_db.exists():
        print(f"[imessage] chat.db not found at {chat_db}", file=sys.stderr)
        sys.exit(1)

    try:
        # Use a read-only URI to avoid locking Messages.app
        con = sqlite3.connect(f"file:{chat_db}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        # "unable to open database file" with an FDA-needed chat.db is the
        # canonical signature of the macOS TCC denial. Surface it as a
        # distinct exit code so the wrapper can label the stage as a
        # setup gap rather than a crash.
        print(f"[imessage] Cannot open chat.db: {e}", file=sys.stderr)
        print(
            "[imessage] Full Disk Access not granted to Estormi — "
            "System Settings → Privacy & Security → Full Disk Access → add Estormi.",
            file=sys.stderr,
        )
        sys.exit(EXIT_NEEDS_FDA)

    con.row_factory = sqlite3.Row
    # Filter on a unit-normalised SECONDS boundary. `message.date` mixes ns
    # (modern) and seconds (legacy) rows in the same column; a nanosecond cutoff
    # (~7e17) is always greater than any seconds-valued row (~7e8), so a recent
    # *legacy* message would be silently excluded forever. Normalise each row to
    # seconds in SQL (matching APPLE_NS_THRESHOLD / apple_ts_to_dt) and compare
    # against the seconds-since-APPLE_EPOCH cutoff.
    cutoff_seconds = (
        datetime.now(tz=timezone.utc) - timedelta(days=days)
    ).timestamp() - APPLE_EPOCH.timestamp()

    # No text filter in SQL: modern messages have NULL `text` and carry the
    # body in `attributedBody`. Empty rows (attachment-only, group events) are
    # dropped after decoding below.
    query = """
        SELECT
            m.guid,
            m.text,
            m.attributedBody,
            m.is_from_me,
            m.date          AS msg_date,
            m.service,
            h.id            AS handle_id,
            c.display_name  AS chat_name,
            c.chat_identifier
        FROM message m
        LEFT JOIN handle h ON h.rowid = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.rowid
        LEFT JOIN chat c ON c.rowid = cmj.chat_id
        WHERE (CASE WHEN m.date > ? THEN m.date / 1000000000.0 ELSE m.date END) >= ?
        ORDER BY (CASE WHEN m.date > ? THEN m.date / 1000000000.0 ELSE m.date END) ASC
    """

    try:
        rows = con.execute(
            query, (APPLE_NS_THRESHOLD, cutoff_seconds, APPLE_NS_THRESHOLD)
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        # chat.db is occasionally corrupt mid-write (Messages.app rotates it,
        # iCloud journaling, etc.). Failing the pipeline stage would alarm the
        # user; exit 0 so the next run retries.
        print(f"[imessage] chat.db unreadable: {exc} — skipping run.", file=sys.stderr)
        con.close()
        sys.exit(0)
    con.close()

    # Track the highest message timestamp we observed — used by
    # watch_and_ingest.sh to advance the watermark precisely instead of
    # relying on `date -u +...` after the fact. Rows are ordered ASC by the
    # unit-normalised timestamp, so the last one (if any) is the real latest;
    # its raw ``msg_date`` may be in seconds or ns and apple_ts_to_dt handles
    # either.
    max_date_raw = rows[-1]["msg_date"] if rows else 0
    max_date_iso = apple_ts_to_dt(max_date_raw).isoformat() if max_date_raw else ""

    count = 0
    failed_decode = 0
    for row in rows:
        text = (row["text"] or "").strip()
        if not text:
            decoded = decode_attributed_body(row["attributedBody"]).strip()
            # Track silent decode failures so a sudden spike (typedstream
            # format change, corrupt blob) shows up in the run log instead
            # of silently halving the day's iMessage corpus.
            if not decoded and row["attributedBody"]:
                failed_decode += 1
            text = decoded
        if not text:
            continue

        # U+FFFC (object replacement char) marks an inline attachment.
        # Strip the marker but keep the surrounding text — dropping the whole
        # message silently lost text-with-attachment messages (e.g. "Look at
        # this ⃞" with a photo). After stripping, an empty body means the
        # message was attachment-only and can be skipped.
        if "￼" in text:
            text = text.replace("￼", "").strip()
            if not text:
                continue

        # Skip OTP / verification code messages — short-lived secrets with no memory value
        if is_otp_message(text):
            continue

        guid = row["guid"] or ""
        is_me = bool(row["is_from_me"])
        msg_date = row["msg_date"] or 0
        handle = row["handle_id"] or ""
        chat_name = row["chat_name"] or row["chat_identifier"] or handle or "unknown"
        service = row["service"] or "iMessage"

        ts = apple_ts_to_dt(msg_date)
        name = "Me" if is_me else (handle or "unknown")

        date_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        # Never print raw message text — even truncated to 60 chars it leaks
        # PII into the connector log. Use a length-only preview.
        print(f"[imessage] {date_str} [{chat_name}] {name}: ({len(text)} chars)")

        if dry_run:
            continue

        safe = _safe_id(guid) if guid else _safe_id(f"{handle}:{msg_date}:{text}")
        STAGING.mkdir(parents=True, exist_ok=True)
        body_path = STAGING / f"{safe}.txt"
        meta_path = STAGING / f"{safe}.meta.json"
        # Atomic two-step write: stage both files as ``.tmp`` siblings, then
        # rename body first and meta last. A crash, OOM-kill, or disk-full
        # event between writes would otherwise leave a ``.txt`` with no
        # metadata (or vice-versa), and the shell loop's
        # ``[ -f "$body_file" ] || continue`` masks orphans into perpetual
        # re-staging on every retry. Meta-last preserves the loop's
        # implicit "have meta ⇒ have body" invariant.
        body_tmp = body_path.with_suffix(".txt.tmp")
        meta_tmp = meta_path.with_suffix(".json.tmp")
        body_tmp.write_text(text, encoding="utf-8")
        meta_tmp.write_text(
            json.dumps(
                {
                    "id": guid,
                    "from": handle,
                    "name": name,
                    "chat_name": chat_name,
                    "timestamp_iso": ts.isoformat(),
                    "chat_id": row["chat_identifier"] or handle,
                    "service": service,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        body_tmp.replace(body_path)
        meta_tmp.replace(meta_path)
        count += 1

    if failed_decode:
        print(
            f"[imessage] WARNING: {failed_decode} message(s) had an attributedBody "
            f"that decoded to empty — skipped.",
            file=sys.stderr,
        )

    # Expose the highest observed message timestamp to the shell wrapper so
    # the watermark can advance to a precise message boundary rather than
    # `date -u` taken some time after the export began.
    if not dry_run and max_date_iso:
        try:
            STAGING.mkdir(parents=True, exist_ok=True)
            # Atomic write: a torn write here can leave a partial timestamp
            # the shell wrapper reads, advancing the watermark past
            # messages we never actually persisted.
            max_date_path = STAGING / "_max_date.txt"
            tmp = max_date_path.with_suffix(".txt.tmp")
            tmp.write_text(max_date_iso, encoding="utf-8")
            tmp.replace(max_date_path)
        except OSError:
            pass

    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch iMessages and stage for ingestion")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--days", type=int, default=DAYS, help=f"Lookback window in days (default {DAYS})"
    )
    args = parser.parse_args()

    n = fetch(days=args.days, dry_run=args.dry_run)
    label = "(dry-run) " if args.dry_run else ""
    print(f"[imessage] {label}Fetched {n} messages.")
