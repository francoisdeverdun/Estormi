#!/usr/bin/env python3
"""Export pending Reminders via EventKit, one .meta.json + .txt per reminder.

Runs through the *bundled* Python interpreter — the same binary the activation
permission probe (``server.permissions`` → ``estormi_ingestion.shared.host.macos_permissions``)
uses to request Reminders access. Because the reading process and the primed
process share one TCC client identity, the grant taken at source activation
carries over to ingestion and the user is not re-prompted mid-run.

This replaces an ad-hoc ``swiftc``-compiled exporter, whose separate (and
per-compile-changing) code identity never inherited the app's grant.

Output dir: ``$STAGING_DIR`` if set, else ``~/estormi-staging/reminders``.
Writes ``_export_complete.flag`` only after every reminder persisted cleanly —
the shell wrapper gates its destructive mark-complete UPDATE on that flag.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    import EventKit  # type: ignore
except ImportError:
    sys.stderr.write("Error: EventKit unavailable; install pyobjc-framework-EventKit\n")
    sys.exit(1)


def _staging_dir() -> Path:
    staging = os.environ.get("STAGING_DIR", "").strip()
    if staging:
        return Path(staging)
    return Path.home() / "estormi-staging" / "reminders"


def _iso_utc(due) -> str:  # noqa: ANN001 — NSDate from EventKit
    """Format an EventKit due NSDate as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), or ""."""
    if due is None:
        return ""
    try:
        ts = due.timeIntervalSince1970()
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001 — a malformed date must not abort the export
        return ""


def _has_time(due_comps) -> bool:  # noqa: ANN001 — NSDateComponents from EventKit
    """True when the reminder carries a time-of-day, not just a calendar date.

    EventKit returns a sentinel (``NSDateComponentUndefined``) for any field the
    user left unset, so a date-only reminder has no valid hour/minute. The exact
    sentinel value varies across macOS/pyobjc versions, so test for a *valid*
    clock value (0–23h / 0–59m) rather than against the sentinel.
    """
    if due_comps is None:
        return False
    return (0 <= due_comps.hour() <= 23) and (0 <= due_comps.minute() <= 59)


def _format_due(due_comps) -> str:  # noqa: ANN001 — NSDateComponents from EventKit
    """Due string for a reminder's due components.

    A reminder with a date but no time is emitted as a *bare* ``YYYY-MM-DD`` —
    the repo's canonical "all day" encoding, identical to how an all-day Google
    Calendar event is stored, and the form ``_is_all_day_raw`` (in
    ``estormi_briefing.day.day``) recognises. The briefing then anchors it on
    that LOCAL calendar day and announces it "toute la journée".

    The previous encoding pinned local midnight and converted to a UTC
    ``…Z`` datetime, which east-of-UTC (e.g. Paris, +02:00) pushed back to
    ``22:00Z`` on the *previous* calendar day. ``_is_all_day_raw`` reads a
    datetime as *timed*, so that string was mis-dated a day early and announced
    as "hier soir" / "en retard d'un jour" for a task actually due today.

    Timed reminders keep their full UTC datetime.
    """
    if due_comps is None:
        return ""
    if _has_time(due_comps):
        return _iso_utc(due_comps.date())
    y, m, d = due_comps.year(), due_comps.month(), due_comps.day()
    if min(y, m, d) <= 0:  # incomplete components — fall back to EventKit's own date
        return _iso_utc(due_comps.date())
    # The date components are already the local calendar day the user picked;
    # emit them verbatim as a bare date — NO timezone conversion (that was the bug).
    return f"{y:04d}-{m:02d}-{d:02d}"


def _safe_id(raw: str) -> str:
    safe = raw.translate(str.maketrans({"/": "_", "\\": "_", ":": "_", " ": "_"}))
    return safe[:200]


def _write_atomic(path: Path, data: bytes) -> None:
    """Write then rename so a torn write never leaves a half-file behind."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _request_access(store) -> bool:  # noqa: ANN001
    done = threading.Event()
    result = {"granted": False}

    def _completion(granted, error):  # noqa: ANN001
        result["granted"] = bool(granted)
        done.set()

    # macOS 14+ split Reminders out of the generic entity-type API.
    store.requestFullAccessToRemindersWithCompletion_(_completion)
    done.wait(timeout=30)
    return result["granted"]


def _fetch_reminders(store, predicate):  # noqa: ANN001
    done = threading.Event()
    holder: dict[str, list] = {"reminders": []}

    def _completion(reminders):  # noqa: ANN001
        holder["reminders"] = list(reminders or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(predicate, _completion)
    done.wait(timeout=120)
    return holder["reminders"]


def main() -> int:
    store = EventKit.EKEventStore.alloc().init()
    if not _request_access(store):
        sys.stderr.write("Error: Reminders access denied\n")
        return 1

    out_dir = _staging_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clear any stale completeness flag before writing — see export_reminders
    # history: a surviving flag during a partial-failure run would re-enable
    # the destructive mark-complete path the wrapper guards against.
    flag_file = out_dir / "_export_complete.flag"
    flag_file.unlink(missing_ok=True)

    predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
        None, None, None
    )
    reminders = _fetch_reminders(store, predicate)

    count = 0
    any_write_failed = False
    for reminder in reminders:
        title = reminder.title() or ""
        notes = reminder.notes() or ""
        list_name = reminder.calendar().title()

        due_comps = reminder.dueDateComponents()
        due_str = _format_due(due_comps)

        safe_id = _safe_id(reminder.calendarItemIdentifier())

        body = f"List: {list_name}\nTitle: {title}"
        if due_str:
            body += f"\nDue: {due_str}"
        body += "\nStatus: pending"
        if notes:
            body += f"\nNotes: {notes}"

        meta = {
            "title": title,
            "date": due_str,
            "list": list_name,
            "completed": False,
            "id": safe_id,
        }
        # Body first, meta last (matching the wrapper's meta→body walk), both
        # atomic. A failed write leaves this reminder out of EXPORTED_IDS, so
        # we set any_write_failed and skip the flag — the wrapper then skips
        # the destructive mark-complete UPDATE rather than losing the row.
        try:
            _write_atomic(out_dir / f"{safe_id}.txt", body.encode("utf-8"))
        except OSError as exc:
            sys.stderr.write(f"Error: failed to write body for {safe_id} — {exc}\n")
            any_write_failed = True
            continue
        try:
            _write_atomic(
                out_dir / f"{safe_id}.meta.json",
                json.dumps(meta, sort_keys=True).encode("utf-8"),
            )
        except OSError as exc:
            sys.stderr.write(f"Error: failed to write meta for {safe_id} — {exc}\n")
            any_write_failed = True
            continue
        count += 1

    if not any_write_failed:
        _write_atomic(flag_file, b"")
    print(f"{count} reminders exported to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
