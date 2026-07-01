"""Per-day personal-corpus fetches feeding the day-vision and the actions list.

Everything the briefing reads back about *the user's own* day: the actionable
calendar/reminders list, the recent WhatsApp tails, the cross-source day-context
window, the health (WHOOP) track, and the near-term events that anchor
correlation. Also the calendar/WhatsApp group-type sets that decide what counts
as actionable vs. context-only.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import aiosqlite

from estormi_briefing.compose.prompts import _is_generated_knowledge_note
from estormi_briefing.day.day import (
    _dedupe_calendar_reminders,
    _format_action,
    _local_when_label,
    _parse_iso_datetime,
    _utc_bounds_for_local_day,
)
from estormi_briefing.io.mcp_io import _fetch_around_mcp

# How many days of look-back (in addition to the briefing day) the day-vision
# correlates over. The window covers the briefing day and the prior this-many
# days — look-back only, so the recap stays on the lead-up to today's events
# without a next-day calendar entry leaking in. The forward correlation horizon
# is a separate, labelled path (see _fetch_upcoming_events).
_BRIEFING_WINDOW_DAYS = int(os.getenv("BRIEFING_WINDOW_DAYS", "2"))

# Event-anchored cross-source correlation (see ``day_vision._correlate_event``):
# the near-term calendar events fetched below are the anchors that search is
# run against. Forward horizon: how far ahead an event may be and still anchor
# correlation. "anchored-now, unbounded-in-time" — a trip in ~2 months must be
# reachable as an anchor so today's chatter about it links up. Anchor set stays
# bounded by dedup + _CORR_MAX_EVENTS, so a long horizon doesn't flood the
# prompt. Both env-tunable.
_CORR_HORIZON_DAYS = int(os.getenv("BRIEFING_CORRELATION_HORIZON_DAYS", "75"))
_CORR_MAX_EVENTS = int(os.getenv("BRIEFING_CORRELATION_MAX_EVENTS", "12"))

# How far back the day-vision pulls WhatsApp conversation tails, and the cap on
# how many chunks reach the prompt. The day-vision judges these directly (there
# is no precomputed pending-reply flag any more), so the window is the only
# recall knob — wide enough to catch a thread from a day or two ago, capped so a
# busy day can't crowd out everything else.
_WA_RECENT_HOURS = float(os.getenv("BRIEFING_WHATSAPP_RECENT_HOURS", "48"))
_WA_RECENT_MAX_CHUNKS = int(os.getenv("BRIEFING_WHATSAPP_RECENT_MAX_CHUNKS", "30"))

# Health (WHOOP) is fetched on its own track so a busy window of mail/chats can't
# crowd the readiness read out of the capped day-context bundle. Keep the most
# recent few cycles (today's recovery + a little trend).
_HEALTH_MAX_CHUNKS = int(os.getenv("BRIEFING_HEALTH_MAX_CHUNKS", "3"))

# Calendar lives under two source slugs: `calendar` (Apple Calendar) and
# `gcal` (Google Calendar). The daily actions and the day-vision context must
# read both — a user on Google Calendar with Apple Calendar disabled would
# otherwise get an empty schedule (see _fetch_daily_actions / _format_action).
_CALENDAR_SOURCES = ("calendar", "gcal")
# Group types that drive the actionable "My Day" schedule list. Must stay a
# subset of the canonical calendar vocabulary (services.calendar_oauth
# .GCAL_GROUP_TYPES) — the only values a calendar can actually be tagged with.
_DAY_CALENDAR_GROUP_TYPES = {"me", "partner", "work", "couple"}
# Group types surfaced to the day-vision as *context* for correlation but kept
# out of the actionable schedule — every non-actionable calendar tag the UI can
# produce. They let the narrative connect a date to an event ("it's Mother's
# Day, and you're seeing your mother tonight") without cluttering the to-do list.
# The prompt treats these as context-only (see knowledge_day_vision.j2). Only
# 'noise' (muted) and 'unknown' (untagged) are left out.
_CONTEXT_CALENDAR_GROUP_TYPES = _DAY_CALENDAR_GROUP_TYPES | {
    "organisation",
    "family",
    "friends",
    "charity",
    "sport",
}
# Actionable WhatsApp threads for "My Day". A subset of the canonical WhatsApp
# vocabulary (services.whatsapp._WA_GROUP_TYPES): the partner ('couple') plus the
# user's family/friends/work circles. Must stay a subset of that producer set —
# 'couple' is the partner's conversation, the single highest-value daily thread.
_DAY_WHATSAPP_GROUP_TYPES = {
    "couple",
    "family",
    "friends",
    "work",
    "unknown",
}
# Wider set admitted to the day-vision *context* (not the actionable pending
# list): generic ``group`` and ``sport`` chats carry real coordination — a
# running crew, a club, an event thread — which the schedule-grade set omits to
# keep the to-do list clean. Correlation needs them, so the context window and
# the event-anchored search below see them too.
_CONTEXT_WHATSAPP_GROUP_TYPES = _DAY_WHATSAPP_GROUP_TYPES | {"group", "sport"}


def _wa_effective_type(chunk: dict) -> str:
    """The classification value the _DAY/_CONTEXT_WHATSAPP sets are written against.

    ``group_type`` used to hold the structural JID fallback (dm/group/broadcast)
    when a chat wasn't semantically categorised; that now lives in its own
    ``chat_kind`` field and ``group_type`` defaults to 'unknown'. Reconstruct the
    pre-split combined value so the briefing's WhatsApp filters behave exactly as
    before: prefer the semantic tag, falling back to the structural kind.
    """
    gt = chunk.get("group_type") or "unknown"
    if gt == "unknown":
        return chunk.get("chat_kind") or "unknown"
    return gt


async def _fetch_daily_actions(db: aiosqlite.Connection, day: date) -> dict:
    """Return {calendar, reminders} for the given local day."""
    after, before = _utc_bounds_for_local_day(day)

    # Completed reminders mask same-titled calendar events (the mirror case
    # below), but only over a short trailing window: without the lower bound a
    # recurring chore completed months ago ("Take out the trash") would hide
    # every future calendar occurrence of the same title forever.
    cur = await db.execute(
        "SELECT title FROM chunks "
        "WHERE source = 'reminders' AND completed = 1 "
        "AND date_ts IS NOT NULL AND datetime(date_ts) <= datetime(?) "
        "AND datetime(date_ts) >= datetime(?, '-7 days')",
        (before, after),
    )
    completed_reminder_titles = {
        " ".join(str(r["title"] or "").lower().split()) for r in await cur.fetchall()
    }
    await cur.close()

    cur = await db.execute(
        "SELECT title FROM chunks "
        "WHERE source = 'reminders' AND completed = 0 "
        "AND date_ts IS NOT NULL AND datetime(date_ts) <= datetime(?)",
        (before,),
    )
    pending_reminder_titles = {
        " ".join(str(r["title"] or "").lower().split()) for r in await cur.fetchall()
    }
    await cur.close()

    cur = await db.execute(
        "SELECT source, title, date, date_ts, end_date_ts, group_type, chat_id_raw, "
        "event_type, event_status, working_location FROM chunks "
        "WHERE source IN ('calendar', 'gcal') AND date_ts IS NOT NULL "
        "AND datetime(date_ts) <= datetime(?) "
        "AND ("
        "  (end_date_ts IS NOT NULL AND datetime(end_date_ts) >= datetime(?)) "
        "  OR (end_date_ts IS NULL AND datetime(date_ts) >= datetime(?)) "
        ") ORDER BY date_ts, title",
        (before, after, after),
    )
    cal_rows = [
        _format_action(r)
        for r in await cur.fetchall()
        if (r["group_type"] or "unknown") in _DAY_CALENDAR_GROUP_TYPES
        and (
            " ".join(str(r["title"] or "").lower().split()) not in completed_reminder_titles
            or " ".join(str(r["title"] or "").lower().split()) in pending_reminder_titles
        )
    ]
    await cur.close()

    cur = await db.execute(
        "SELECT source, title, date, date_ts, group_type, chat_id_raw FROM chunks "
        "WHERE source = 'reminders' AND date_ts IS NOT NULL "
        "AND datetime(date_ts) <= datetime(?) AND completed = 0 ORDER BY date_ts, title",
        (before,),
    )
    rem_rows = [_format_action(r, after_utc=after) for r in await cur.fetchall()]
    await cur.close()
    cal_rows, rem_rows = _dedupe_calendar_reminders(cal_rows, rem_rows)

    return {
        "calendar": cal_rows,
        "reminders": rem_rows,
    }


async def _fetch_recent_whatsapp(day: date, hours: float = _WA_RECENT_HOURS) -> list[dict]:
    """Recent WhatsApp conversation chunks (the ``hours`` leading up to the end
    of the briefing day) for the day-vision.

    Replaces the old pending-reply prefilter. Rather than a coarse "the last
    message isn't from me" flag computed at ingestion — which was content-blind
    (an answer to your own question still tripped it) and went stale the moment
    you replied — this hands the day-vision the recent conversation tails and
    lets it judge what actually needs a reply (the prompt's WhatsApp discipline
    rules do the deciding). Newest first, capped so a noisy day can't flood the
    prompt.
    """
    window_days = max(1, int(hours // 24) + 1)
    chunks = await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": window_days,
            # Recent tails are a look-back; cap the look-ahead so a clock-skewed
            # or rebuilt-past-day run can't admit a chat dated after the day.
            "forward_days": 0,
            "corpus": "personal",
            "sources": ["whatsapp"],
            "limit": 200,
        },
        timeout=12.0,
    )
    # Anchor the recency cutoff on the END of the briefing day, not wall-clock
    # now(): re-building a past day (ESTORMI_BRIEFING_DATE override) would
    # otherwise intersect ``now − hours`` with a window centred days earlier —
    # empty, silently dropping the WhatsApp section from the rebuilt briefing.
    # For a same-day run the day end sits just ahead of now, so it's equivalent.
    _, before = _utc_bounds_for_local_day(day)
    before_dt = _parse_iso_datetime(before) or datetime.now(timezone.utc)
    cutoff = before_dt - timedelta(hours=hours)

    def _ts(chunk: dict) -> datetime | None:
        # Compare real instants, not raw ISO text: a non-UTC offset on a
        # date_ts would otherwise mis-window the recent tail under a string
        # compare. Skip chunks whose timestamp won't parse.
        return _parse_iso_datetime(chunk.get("date_ts") or chunk.get("date"))

    parsed = [
        (dt, chunk)
        for chunk in chunks
        if _wa_effective_type(chunk) in _DAY_WHATSAPP_GROUP_TYPES
        and (dt := _ts(chunk)) is not None
        and cutoff <= dt <= before_dt
    ]
    parsed.sort(key=lambda p: p[0], reverse=True)
    return [c for _, c in parsed[:_WA_RECENT_MAX_CHUNKS]]


async def _fetch_day_context_chunks(day: date, limit: int = 12) -> list[dict]:
    """Time-window bundle so the day recap correlates across sources.

    Instead of a keyword search, pull every *personal* chunk whose date
    overlaps a window covering the briefing day and the prior
    ``BRIEFING_WINDOW_DAYS`` (look-back only — it never crosses into tomorrow,
    so next-day events can't leak into today's recap) — calendar, mail,
    reminders, chats, notes, documents together. Items about the same
    real-world thing cluster in time, so handing the model one coherent window
    lets it weave threads (a mail, the event, the reminder, the chat) rather
    than guessing links from disjoint keyword hits.
    """
    chunks = await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": _BRIEFING_WINDOW_DAYS,
            # Look back over the lead-up, but never cross into tomorrow: the
            # window is the briefing day plus the prior _BRIEFING_WINDOW_DAYS,
            # so a next-day calendar entry can't pose as today's context. The
            # forward correlation horizon has its own labelled path
            # (_fetch_upcoming_events / day_vision._correlate_event).
            "forward_days": 0,
            "corpus": "personal",
            "limit": max(limit * 4, 60),
        },
        timeout=12.0,
    )

    def relevant(chunk: dict) -> bool:
        source = chunk.get("source") or ""
        # Briefings are delivered to the vault only — never re-ingested as
        # `briefing`-source chunks (see the writer note below). This guard is
        # therefore belt-and-suspenders against any legacy briefing chunk from
        # an older build, and it also drops generated knowledge notes — either
        # way, never feed the briefing's own output back into tomorrow's
        # context, or it slowly regurgitates itself.
        if source == "briefing" or _is_generated_knowledge_note(chunk):
            return False
        group_type = chunk.get("group_type") or "unknown"
        if source in _CALENDAR_SOURCES:
            # Context window is wider than the schedule: it also admits holidays
            # / org calendars so the day-vision can correlate them with personal
            # events (the prompt keeps them context-only).
            return group_type in _CONTEXT_CALENDAR_GROUP_TYPES
        if source == "whatsapp":
            return _wa_effective_type(chunk) in _CONTEXT_WHATSAPP_GROUP_TYPES
        return True

    def _is_date_anchor(chunk: dict) -> bool:
        """A holiday / org-calendar date — a rare correlation hook."""
        return (chunk.get("source") in _CALENDAR_SOURCES) and (
            (chunk.get("group_type") or "") == "organisation"
        )

    relevant_chunks = [chunk for chunk in chunks if relevant(chunk)]
    # Pull rare date-anchors (holidays / org-calendar dates) to the front so the
    # capped window can't crowd them out behind dozens of chat/mail chunks —
    # they're what lets the day-vision tie a date to a personal event. Stable
    # sort, so everything else keeps its fetch_around order.
    relevant_chunks.sort(key=lambda c: 0 if _is_date_anchor(c) else 1)
    return relevant_chunks[:limit]


async def _fetch_health_chunks(day: date) -> list[dict]:
    """The latest WHOOP cycle(s) going into the briefing day — the body's state.

    Fetched on its OWN track (``sources=['whoop']``), not through the capped
    day-context bundle: on a busy day the readiness read sits dozens of chunks
    deep in the newest-first window (mail, chats) and gets dropped by the cap,
    so the briefing never saw it. Keep the most recent few — today's recovery
    plus a little trend.
    """
    chunks = await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": _BRIEFING_WINDOW_DAYS,
            "corpus": "personal",
            "sources": ["whoop"],
            "limit": 10,
        },
        timeout=12.0,
    )
    # Drop cycles that START on or after the briefing day's local midnight. A
    # WHOOP ``date_ts`` is the cycle START — the evening BEFORE the labelled day
    # — so the cycle *labelled* the briefing day is stamped the prior evening and
    # rides through, while a NEXT-day-labelled cycle (stamped ~22:xx of the
    # briefing day itself) is in the future for this briefing and must not become
    # ``health[0]`` on a past-day rebuild or a post-midnight run. Compare real
    # instants against the tz-aware day-start, not raw ISO text.
    after, _ = _utc_bounds_for_local_day(day)
    after_dt = _parse_iso_datetime(after)
    if after_dt is not None:
        chunks = [
            c
            for c in chunks
            if (_parse_iso_datetime(c.get("date_ts") or c.get("date")) or after_dt) < after_dt
        ]
    # Sort by real instant, not raw ISO text: WHOOP is all-UTC today so a string
    # sort happens to agree, but a non-UTC offset (matching the fetch_around fix)
    # would mis-order under lexical compare. Mirror _fetch_recent_whatsapp.
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    chunks.sort(
        key=lambda c: _parse_iso_datetime(c.get("date_ts") or c.get("date")) or _epoch,
        reverse=True,
    )
    return chunks[:_HEALTH_MAX_CHUNKS]


async def _fetch_upcoming_events(day: date) -> list[dict]:
    """Near-term actionable calendar events (today .. +HORIZON) to anchor
    correlation on. Recurring titles collapse to one, so a weekly "Lunch run"
    is searched once, not per occurrence.
    """
    chunks = await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": _CORR_HORIZON_DAYS,
            "corpus": "personal",
            "sources": list(_CALENDAR_SOURCES),
            "limit": 200,
        }
    )
    after, _ = _utc_bounds_for_local_day(day)
    after_dt = _parse_iso_datetime(after)
    seen: set[str] = set()
    events: list[dict] = []
    for chunk in chunks:
        if (chunk.get("group_type") or "unknown") not in _DAY_CALENDAR_GROUP_TYPES:
            continue
        date_ts = chunk.get("date_ts") or ""
        # Compare real instants, not raw ISO text: stored ``date_ts`` carries its
        # source offset (gcal feeds ``+02:00``), so a string ``<`` would mis-window
        # offset-bearing events around the day boundary.
        dt = _parse_iso_datetime(date_ts)
        if dt is None or (after_dt is not None and dt < after_dt):
            continue  # only today and forward, not the past half-window
        title = (chunk.get("title") or "").strip()
        key = " ".join(title.lower().split())
        if not title or key in seen:
            continue
        seen.add(key)
        # The event body (gcal shape: title\nstart → end\nlocation\ndescription)
        # often carries the richest correlation vocabulary — "achat groom gr200
        # chez cogefrem" — that the bare title lacks. Hand it to the anchor so
        # the correlation query embeds the real subject, not just the label.
        body_lines = [ln.strip() for ln in (chunk.get("text") or "").split("\n")[2:] if ln.strip()]
        events.append(
            {
                "title": title,
                "detail": " ".join(" ".join(body_lines).split())[:220],
                "when_label": _local_when_label(date_ts, chunk.get("date")),
                "date_ts": date_ts,
                "_dt": dt,
            }
        )
    events.sort(key=lambda e: e["_dt"])
    for event in events:
        event.pop("_dt", None)
    return events[:_CORR_MAX_EVENTS]
