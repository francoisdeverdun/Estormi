"""
Google Calendar incremental sync.

State stored in the ``settings`` SQLite table:

* ``google_calendar_selected_ids`` — JSON array of calendar IDs to sync
  (empty / missing means: sync all calendars the user has access to).
* ``google_calendar_sync_token`` — JSON object mapping ``calendar_id`` →
  Google's opaque ``nextSyncToken``. Per-calendar so a 410 on one calendar
  forces a full resync only for that one.

Strategy:
* First run per calendar: ``events.list(calendarId, timeMin=now-DAYS_WINDOW,
  timeMax=now+DAYS_FORWARD, singleEvents=True, orderBy=startTime)`` paginated
  until we get ``nextSyncToken``. The bounded window stops "repeats forever"
  series from fanning out into far-future instances; ``orderBy`` makes the kept
  representative deterministic across resyncs.
* Subsequent runs: ``events.list(calendarId, syncToken=stored_token)``
  returning only deltas. HTTP 410 ⇒ stored token discarded, full resync.
* Events with ``status == 'cancelled'`` ⇒ delete the corresponding chunk
  (looked up by ``source_id``).
* A recurring series is stored as ONE chunk keyed on its master
  (``recurringEventId``), not on the per-instance id — so a full resync that
  picks a different instance maps to the same chunk instead of accumulating a
  new one each time.

Public entry point: :func:`sync` returns a dict of counts.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import sys as _sys
from typing import Any

import structlog

from estormi_ingestion.shared.config import mcp_url
from estormi_ingestion.shared.emit import content_base_hash
from estormi_ingestion.shared.http_client import post_chunk
from estormi_ingestion.shared.paths import estormi_db_path
from memory_core.pii_filter import filter_pii
from memory_core.sanitizer import strip_calendar_sync_footer

from . import auth as gcal_auth

log = structlog.get_logger()

SETTING_SELECTED = "google_calendar_selected_ids"
SETTING_SYNC_TOKEN = "google_calendar_sync_token"
SETTING_GROUP_TYPES = "google_calendar_group_types"
SOURCE = "gcal"

# First-sync lookback window (only applies before a syncToken exists).
# Driven by the Manage modal's historic-depth picker via GCAL_DAYS_WINDOW;
# 90 days by default. Incremental syncs afterwards use the sync token.
DAYS_WINDOW = int(os.environ.get("GCAL_DAYS_WINDOW", "90"))

# Forward horizon for the first sync. ``singleEvents=True`` expands recurring
# events into instances; WITHOUT a ``timeMax`` Google walks "repeats forever"
# series far into the future (year-2038 instances were observed), so the
# expansion — and the per-series chunk churn it drives — is bounded here.
DAYS_FORWARD = int(os.environ.get("GCAL_DAYS_FORWARD", "365"))

# Events go through the same /ingest_chunk REST endpoint as every other
# connector — so they are chunked, embedded, and indexed in Qdrant (a
# direct SQLite insert would skip vectorisation, leaving events
# unsearchable). Cancelled events are retracted via /ingest_delete.
MCP_URL = mcp_url()


# ─── Settings helpers (work with both sqlite3.Connection and aiosqlite via
# the synchronous DB-API surface) ──────────────────────────────────────────


def _setting_get(db, key: str) -> str | None:
    cur = db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if hasattr(cur, "close"):
        cur.close()
    if not row:
        return None
    # sqlite3.Row or tuple
    try:
        return row["value"]
    except (TypeError, IndexError, KeyError):
        return row[0]


def _setting_set(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    if hasattr(db, "commit"):
        db.commit()


def get_selected_calendar_ids(db) -> list[str]:
    raw = _setting_get(db, SETTING_SELECTED)
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except (ValueError, TypeError):
        pass
    return []


def _load_group_types(db) -> dict[str, str]:
    """Read the {calendar_id: group_type} map the Settings UI stores as a JSON
    blob. The tag rides onto every event chunk so search and the briefing can
    tell a work calendar from a family one."""
    raw = _setting_get(db, SETTING_GROUP_TYPES)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return {str(k): str(v) for k, v in val.items()}
    except (ValueError, TypeError):
        pass
    return {}


def _load_sync_tokens(db) -> dict[str, str]:
    raw = _setting_get(db, SETTING_SYNC_TOKEN)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return {str(k): str(v) for k, v in val.items()}
    except (ValueError, TypeError):
        pass
    return {}


def _save_sync_tokens(db, tokens: dict[str, str]) -> None:
    _setting_set(db, SETTING_SYNC_TOKEN, json.dumps(tokens))


def _iso_now_z() -> str:
    """UTC now as a ``YYYY-MM-DDTHH:MM:SSZ`` string (the watermark format the
    SourcesPanel compacts to ``MM-DD HH:MM``)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_watermark(db, fetched_at: str) -> None:
    """Record WHEN the sync token was last persisted, in ``ingestion_watermarks``.

    gcal has no per-event timestamp watermark — it syncs by opaque sync token —
    so the SourcesPanel row would otherwise have no freshness signal to show but
    the literal "sync tokens" placeholder. Stamping the last-save time here lets
    the row display the actual date the cursor last advanced. Best-effort: a
    cosmetic freshness stamp must never abort an otherwise-successful sync (e.g.
    an older DB missing the table), so failures are logged, not raised.
    """
    try:
        db.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at, last_item_id) "
            "VALUES (?, ?, NULL) "
            "ON CONFLICT(source) DO UPDATE SET last_fetched_at = excluded.last_fetched_at",
            (SOURCE, fetched_at),
        )
        if hasattr(db, "commit"):
            db.commit()
    except Exception as exc:  # noqa: BLE001 — freshness stamp is non-critical
        log.warning("gcal: could not stamp watermark: %s", exc)


def _looks_like_room_code(location: str) -> bool:
    """True for corporate-calendar room / desk booking codes.

    Google returns these in the event ``location`` field — e.g.
    "US-HQ-5-OPEN SPACE West", "GB-Office2 - Floor 4 Desk 12". They are not
    meaningful places; keeping them out of the chunk text stops semantic
    search from treating desk identifiers as locations.
    """
    v = (location or "").strip()
    if not v:
        return False
    if re.match(r"^[A-Z]{2}-[A-Za-z0-9]", v):  # site prefix: FR- ES-
        return True
    if re.search(r"\b\d+-\d+-\w", v):  # numeric desk segment: 45-4-L
        return True
    if re.search(r"\bopen\s+space\b", v, re.IGNORECASE):
        return True
    return False


# Human labels for Google's working-location event types. The synced-copy
# ("Acme Corp (auto-copy)") calendar stamps the day's location onto each
# event's *private* extended properties — it is not in any visible field — so
# this is the only place the remote/on-site signal enters Estormi.
_WORKING_LOCATION_TYPES = {
    "homeOffice": "home office",
    "officeLocation": "office",
    "customLocation": "custom location",
}


def _working_location(event: dict[str, Any]) -> str:
    """The day's working location for this event, or "" when none is set.

    The auto-copy calendar writes ``dayWorkingLocationType`` (homeOffice /
    officeLocation / customLocation) and a human ``dayWorkingLocationLabel``
    (e.g. "Home office", "HQ-3rd-floor") into the event's private
    extended properties. All events on a day share the same value; absence
    means no location was set that day. Rendered as ``<label> (<type>)`` —
    e.g. ``Home office (home office)`` — falling back to whichever half exists.
    """
    private = ((event.get("extendedProperties") or {}).get("private")) or {}
    label = (private.get("dayWorkingLocationLabel") or "").strip()
    kind = _WORKING_LOCATION_TYPES.get((private.get("dayWorkingLocationType") or "").strip(), "")
    if label and kind:
        return f"{label} ({kind})"
    return label or kind


def _all_day_end_inclusive(end_date: str) -> str:
    """Convert Google's EXCLUSIVE all-day ``end.date`` to the inclusive last day.

    Google Calendar's all-day end is exclusive: a one-day event on the 15th
    carries ``end.date == "2024-03-16"``. Stored verbatim, that names the day
    AFTER the event, so every all-day item (holidays, OOO, trips) over-includes
    by one day into the next local day's briefing/retrieval window. Decrement by
    one day so the stored ``end_date_ts`` is inclusive like ``start.date``, kept
    a bare ``YYYY-MM-DD`` so the all-day signal survives (``_is_all_day_raw``).

    A non-parseable value rides through unchanged — a malformed end must not
    abort the run.
    """
    try:
        d = _dt.date.fromisoformat(end_date)
    except ValueError:
        return end_date
    return (d - _dt.timedelta(days=1)).isoformat()


def _event_body(event: dict[str, Any]) -> tuple[str, str, str, str]:
    """Render an event into (title, start, end, ingest-text).

    ``start`` / ``end`` preserve Google's authoritative all-day signal: a timed
    event yields a full ISO datetime (``start.dateTime``), an all-day event a
    bare ``YYYY-MM-DD`` (``start.date``). That bare form rides through to the
    chunk ``date`` column untouched, so the Briefing reconstructs "all day" from
    the missing time component instead of a midnight-clock heuristic that can't
    tell a genuine 00:00 commitment from an all-day block (the reconstruction is
    ``_is_all_day_raw`` in ``estormi_briefing.day.day``). Which key carries the
    start — ``start.date`` vs ``start.dateTime`` — is Google's only reliable
    discriminator, so we must never collapse the bare date to a timestamp here.

    The "maybe" RSVP (status=tentative) and the day's working location are no
    longer stamped into this text — they ride to the server as structured
    chunk fields (see ``_post_event``), so the Briefing reads them as columns
    rather than parsing them back out. The body keeps the briefing's
    location convention: the line right after the ``→`` time line is the
    event location (room codes already stripped).
    """
    title = event.get("summary") or "(no title)"
    # Cross-calendar mirroring tools append a machine footer ("Copie
    # synchronisée automatiquement. Source event ID: …") to every copied
    # event's description — plumbing that pollutes embeddings and leaks into
    # briefing prose. Strip it before the text is chunked.
    description = strip_calendar_sync_footer(event.get("description") or "")
    location = event.get("location") or ""
    if location and _looks_like_room_code(location):
        location = ""
    start = (
        (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
    )
    end_obj = event.get("end") or {}
    if end_obj.get("dateTime"):
        end = end_obj["dateTime"]
    elif end_obj.get("date"):
        # All-day end.date is EXCLUSIVE — decrement to the last covered day so it
        # is inclusive like start.date (see ``_all_day_end_inclusive``).
        end = _all_day_end_inclusive(end_obj["date"])
    else:
        end = ""
    text = f"{title}\n{start} → {end}\n{location}\n{description}".strip()

    # Google Calendar event bodies routinely include phone numbers, emails,
    # and credentials (dial-in PINs, vendor portal logins). Filter once on
    # the rendered text before it leaves this process.
    text = filter_pii(text)
    return title, start, end, text


# Per-event outcomes. ERROR is distinct from SKIPPED so a calendar-wide
# network failure (every POST erroring) is counted as errors rather than
# silently looking like "0 ingested, 0 errors".
POST_INGESTED = "ingested"
POST_SKIPPED = "skipped"
POST_ERROR = "error"


def _delete_event(event_id: str) -> str:
    """Retract a cancelled event via /ingest_delete (SQLite + Qdrant).

    Returns POST_INGESTED if a row was removed, POST_SKIPPED if there was
    nothing to delete, or POST_ERROR on a transient failure.
    """
    try:
        # post_chunk gives us shared.http_client's exponential-backoff retry
        # on transient connection errors and 5xx, so one Google → MCP hiccup
        # does not silently drop the delete (and the next syncToken pull
        # would never re-deliver the cancellation).
        resp = post_chunk(
            f"{MCP_URL}/ingest_delete",
            {"source": SOURCE, "source_id": event_id},
            timeout=30,
        )
        resp.raise_for_status()
        return POST_INGESTED if int(resp.json().get("deleted", 0)) > 0 else POST_SKIPPED
    except Exception as exc:  # noqa: BLE001 — one bad delete must not abort the run
        log.warning("gcal: delete failed for %s: %s", event_id, exc)
        return POST_ERROR


def _post_event(
    event: dict[str, Any], calendar_id: str, group_type: str, source_id: str | None = None
) -> str:
    """POST one event to /ingest_chunk.

    Returns POST_INGESTED if it was stored, POST_SKIPPED if the server
    skipped it as an unchanged duplicate, or POST_ERROR on a transient
    failure (the run continues, but the caller counts it as an error).

    ``source_id`` defaults to the event's own id; for a recurring instance the
    caller passes the master (``recurringEventId``) so the whole series maps to
    one stable chunk instead of a new one per resync.

    ``calendar_id`` rides along as ``chat_id_raw`` so the event can be
    re-tagged later when the user changes the calendar's group_type; the
    current ``group_type`` is stamped on the chunk the same way.

    /ingest_chunk is idempotent: a same-content_hash event is skipped, a
    changed event replaces the prior row for that source_id, and Qdrant
    vectorisation happens server-side."""
    event_id = event["id"]
    source_id = source_id or event_id
    title, start, end, text = _event_body(event)
    # Fold source_id in (content_base_hash): the server dedups GLOBALLY on
    # content_hash, so a shared event appearing byte-identical in two calendars
    # would otherwise collide and the second copy be dropped — losing its
    # distinct calendar identity (chat_id_raw / group_type).
    content_hash = content_base_hash(source_id, text)
    # Structured event facts the Briefing reads as chunk fields. `eventType`
    # distinguishes a real meeting from an absence (outOfOffice) or a blocked
    # work slot (focusTime); a "maybe" RSVP arrives as status=tentative; the
    # working location is the day-level label from the extended properties.
    try:
        # post_chunk = shared.http_client with retry/backoff; same rationale
        # as _delete_event above — a transient 5xx must not silently drop
        # an event that the syncToken cursor won't replay.
        resp = post_chunk(
            f"{MCP_URL}/ingest_chunk",
            {
                "text": text,
                "source": SOURCE,
                "source_id": source_id,
                "title": title,
                "date": start,
                "end_date_ts": end,
                "content_hash": content_hash,
                "chat_id_raw": calendar_id,
                "group_type": group_type,
                "event_type": event.get("eventType") or "default",
                "event_status": event.get("status") or "confirmed",
                "working_location": _working_location(event),
                "meta": {"pii_filtered": True},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return POST_SKIPPED if resp.json().get("status") == "skipped" else POST_INGESTED
    except Exception as exc:  # noqa: BLE001 — one bad event must not abort the run
        log.warning("gcal: ingest failed for %s: %s", event_id, exc)
        return POST_ERROR


# ─── Google API wrapper ────────────────────────────────────────────────────


def _build_service(credentials):
    from googleapiclient.discovery import build  # type: ignore

    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _list_user_calendars(service) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        out.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _is_http_410(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 410:
        return True
    return "410" in str(exc)


def _sync_one_calendar(
    service,
    calendar_id: str,
    sync_token: str | None,
    group_type: str = "unknown",
):
    """Sync a single calendar. Returns (ingested, deleted, errors, next_token).

    Wraps the page loop in a guarded retry so a 410 (sync token invalidated)
    triggers exactly one fresh full re-sync. A second consecutive 410 is a
    real server-side problem and is raised — never silently re-recurse.
    """
    max_410_retries = 1
    attempt = 0
    while True:
        try:
            return _sync_one_calendar_inner(service, calendar_id, sync_token, group_type)
        except Exception as exc:  # noqa: BLE001
            if _is_http_410(exc) and attempt < max_410_retries:
                attempt += 1
                log.warning(
                    "410 Gone for %s — clearing sync token (retry %d/%d)",
                    calendar_id,
                    attempt,
                    max_410_retries,
                )
                sync_token = None
                continue
            raise


def _sync_one_calendar_inner(
    service,
    calendar_id: str,
    sync_token: str | None,
    group_type: str,
):
    """Inner sync loop — see _sync_one_calendar for the 410-retry wrapper."""
    ingested = deleted = errors = 0
    seen_recurring_masters: set = set()
    page_token = None
    next_sync_token: str | None = None

    while True:
        params: dict[str, Any] = {"calendarId": calendar_id, "showDeleted": True}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["singleEvents"] = True
            # orderBy=startTime (only valid with singleEvents) makes the kept
            # instance per recurring master deterministic — the earliest in
            # window — so the same representative is chosen every full resync.
            params["orderBy"] = "startTime"
            now = _dt.datetime.now(_dt.timezone.utc)
            params["timeMin"] = (now - _dt.timedelta(days=DAYS_WINDOW)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            # Bound forward expansion so "repeats forever" series don't fan out
            # into far-future instances.
            params["timeMax"] = (now + _dt.timedelta(days=DAYS_FORWARD)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        if page_token:
            params["pageToken"] = page_token

        resp = service.events().list(**params).execute()

        for event in resp.get("items", []):
            event_id = event.get("id")
            if not event_id:
                continue
            if event.get("status") == "cancelled":
                # Recurring series are stored under the master id (not the
                # per-instance id) — match the key used at ingest time.
                # NOTE: this retracts the entire series chunk for a single
                # cancelled occurrence; per-occurrence granularity requires a
                # one-chunk-per-instance design (out of scope here).
                delete_key = event.get("recurringEventId") or event_id
                outcome = _delete_event(delete_key)
                if outcome == POST_INGESTED:
                    deleted += 1
                elif outcome == POST_ERROR:
                    errors += 1
                continue
            master = event.get("recurringEventId")
            if master:
                if master in seen_recurring_masters:
                    continue
                seen_recurring_masters.add(master)

            # A recurring series is stored as ONE chunk keyed on its master id,
            # not on the per-instance id (e.g. ``master_20380520``). The kept
            # instance only supplies the representative content. Without this,
            # each full resync picked a different instance id and left a fresh
            # chunk behind the old one — the vault grew without bound.
            outcome = _post_event(event, calendar_id, group_type, source_id=master or event_id)
            if outcome == POST_INGESTED:
                ingested += 1
            elif outcome == POST_ERROR:
                errors += 1

        page_token = resp.get("nextPageToken")
        next_sync_token = resp.get("nextSyncToken") or next_sync_token
        if not page_token:
            break

    return ingested, deleted, errors, next_sync_token


def sync(db=None, **kwargs) -> dict[str, int]:
    """Run an incremental sync. Returns counts dict."""
    counts = {"ingested": 0, "deleted": 0, "calendars": 0, "errors": 0}

    if db is None:
        db = sqlite3.connect(estormi_db_path())

    creds = gcal_auth.get_credentials()
    if creds is None:
        log.warning("no google_calendar credentials available")
        counts["errors"] = 1
        return counts

    service = _build_service(creds)
    selected = get_selected_calendar_ids(db)
    if selected:
        cal_ids = selected
    else:
        cal_ids = [c["id"] for c in _list_user_calendars(service)]

    tokens = _load_sync_tokens(db)
    group_types = _load_group_types(db)

    for cal_id in cal_ids:
        try:
            ingested, deleted, errors, next_token = _sync_one_calendar(
                service,
                cal_id,
                tokens.get(cal_id),
                group_types.get(cal_id, "unknown"),
            )
            counts["ingested"] += ingested
            counts["deleted"] += deleted
            # Per-event POST/delete failures are swallowed inside
            # _sync_one_calendar so one bad event cannot abort the run, but
            # they MUST surface here — otherwise a calendar-wide outage
            # looks like a clean "0 ingested, 0 errors" and the pipeline stage
            # is marked green despite ingesting nothing.
            counts["errors"] += errors
            counts["calendars"] += 1
            # Only persist the new syncToken when EVERY event in the page
            # set was either stored or cleanly skipped. Saving it after a
            # partial failure would advance Google's delta cursor past
            # events we never persisted — silent permanent loss, since
            # syncToken pulls only return changes since the last token.
            if next_token and errors == 0:
                tokens[cal_id] = next_token
            elif next_token:
                log.warning(
                    "gcal: skipping sync-token save for %s — %d POST error(s) "
                    "in this window; calendar will re-pull on next run",
                    cal_id,
                    errors,
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("sync failed for %s: %s", cal_id, exc)
            counts["errors"] += 1

    _save_sync_tokens(db, tokens)
    # Stamp the last successful sync time so the SourcesPanel shows a real
    # freshness date instead of a "sync tokens" placeholder. Mirrors whoop's
    # set_watermark(now): a clean run confirms the calendars are current as of
    # now, whether or not any event changed. We deliberately do NOT gate this on
    # a sync-token advancing — gcal's first-sync request carries timeMin/timeMax/
    # orderBy, and Google suppresses nextSyncToken whenever those restrictions
    # are present, so a usable token is rarely returned and gating on it would
    # leave the date perpetually blank. Only a fully clean run that touched at
    # least one calendar stamps; any error holds the date back for a retry.
    if counts["errors"] == 0 and counts["calendars"] > 0:
        _stamp_watermark(db, _iso_now_z())
    return counts


# ─── CLI entry point ────────────────────────────────────────────────────────
#
# Invoked by ``scripts/daily_ingestion.sh`` (the ``gcal`` stage) and by the
# per-source ▶ play button in the SPA. Prints a one-line summary on success
# and exits non-zero on any sync error so the pipeline marks the stage failed.

# Exit code reserved for "stored refresh token is dead — re-auth required".
# The pipeline still treats it as a failure (non-zero), but the value lets a
# future UI layer distinguish "needs re-auth" from "transient API error".
_EXIT_REAUTH = 2


def _main() -> int:
    # A stored-but-revoked refresh token surfaces as ``get_credentials() → None``
    # inside ``sync()`` (``auth.get_credentials`` refreshes eagerly and returns
    # None on ``invalid_grant``). Probe it up front so the pipeline can tell "needs
    # re-auth" (exit 2) from a transient API error (exit 1) — mirrors
    # ``whoop/sync.py``. ``sync()`` swallows every per-calendar error and never
    # re-raises, so a RefreshError can never propagate here.
    if gcal_auth.load_token() is not None and gcal_auth.get_credentials() is None:
        print(
            "[gcal] Google refresh token expired or revoked — "
            "re-authenticate via Settings → Google Calendar.",
            flush=True,
        )
        return _EXIT_REAUTH
    counts = sync()
    print(
        f"[gcal] calendars={counts['calendars']} ingested={counts['ingested']} "
        f"deleted={counts['deleted']} errors={counts['errors']}"
    )
    return 1 if counts.get("errors") else 0


if __name__ == "__main__":
    _sys.exit(_main())
