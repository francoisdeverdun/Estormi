"""Code-rendered schedule strip for the "My day" section.

The timeline puts the bare facts of the day — event times, titles, and the
free slots between them — at the top of the section, rendered
deterministically by code. The LLM prose below then only has to carry
insight (correlations, stakes, advice): a model can hallucinate an hour,
this strip cannot, so the reader always has a trustworthy skeleton of the
day even when the prose drifts.

Pure functions, no I/O: the orchestrator feeds parsed calendar events in and
splices the returned HTML into the note.
"""

from __future__ import annotations

from datetime import date, datetime

from estormi_briefing.day.day import LOCAL_TZ

# Inline styles mirror the iOS CSS tokens (the note renders in contexts that
# strip <style> blocks): gold accents on dark for the times and the strip's
# border, muted gray for the de-emphasised free slots.
_GOLD_BORDER = "#8A7142"
_GOLD_TIME = "#C8A96B"
_MUTED_GRAY = "#6b7280"

# The strip is a glance, not a second agenda — past this many rows it stops
# being scannable, and a day that busy is better served by the prose anyway.
_MAX_ROWS = 16


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _as_dt(value: object) -> datetime | None:
    """The datetime, or ``None`` for anything else — events arrive from
    upstream parsing that may have failed on one bound."""
    return value if isinstance(value, datetime) else None


def free_slots(
    events: list[dict],
    day: date,
    *,
    start_hour: int = 8,
    end_hour: int = 21,
    min_minutes: int = 45,
) -> list[dict]:
    """Free gaps between events within the working window.

    Busy intervals are merged before the gap walk: an event nested inside a
    longer one, or two back-to-back meetings, must not open a phantom slot.
    A point event (``end <= start``) occupies nothing. Events reaching
    outside the window still truncate it — a 07:30–09:00 meeting eats the
    08:00–09:00 part. Gaps shorter than ``min_minutes`` are noise, not
    usable time, so they are dropped.

    ``events``: ``[{"title": str, "start": datetime, "end": datetime}, ...]``
    with tz-aware datetimes. Returns
    ``[{"start": datetime, "end": datetime, "minutes": int}, ...]`` sorted
    chronologically, bounds in ``LOCAL_TZ``.
    """
    window_start = datetime(day.year, day.month, day.day, start_hour, tzinfo=LOCAL_TZ)
    window_end = datetime(day.year, day.month, day.day, end_hour, tzinfo=LOCAL_TZ)
    if window_end <= window_start:
        return []

    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        start, end = _as_dt(event.get("start")), _as_dt(event.get("end"))
        if start is None or end is None or end <= start:
            continue  # unparseable or point event: occupies no time
        busy.append((start.astimezone(LOCAL_TZ), end.astimezone(LOCAL_TZ)))
    busy.sort()

    merged: list[tuple[datetime, datetime]] = []
    for start, end in busy:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    slots: list[dict] = []
    cursor = window_start
    for start, end in merged:
        if start >= window_end:
            break
        if end <= cursor:
            continue  # already swallowed by the window clamp
        if start > cursor:
            _append_slot(slots, cursor, start, min_minutes)
        cursor = end
        if cursor >= window_end:
            return slots
    _append_slot(slots, cursor, window_end, min_minutes)
    return slots


def _append_slot(slots: list[dict], start: datetime, end: datetime, min_minutes: int) -> None:
    minutes = int((end - start).total_seconds() // 60)
    if minutes >= min_minutes:
        slots.append({"start": start, "end": end, "minutes": minutes})


def _fmt_minutes(minutes: int) -> str:
    """``50 min``, ``2 h``, ``1 h 05`` — the shortest exact spelling."""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest:02d}"


def timeline_html(events: list[dict], slots: list[dict], labels: dict[str, str]) -> str:
    """Compact HTML schedule strip. labels: {"free_slot": ..., "all_day": ...}.

    Every piece of text goes through ``_esc``: event titles come from
    calendar invitations the user does not author alone, so a title is an
    injection vector into the note's HTML.
    """
    # (sort_key, row_html) pairs; keys are tuples so the no-time rows (0,)
    # pin to the top and the timed rows (1, datetime) order chronologically.
    rows: list[tuple[tuple, str]] = []
    for event in events:
        title = str(event.get("title") or "").strip()
        if not title:
            continue  # a row with no label is dead weight on the strip
        start, end = _as_dt(event.get("start")), _as_dt(event.get("end"))
        if start is None:
            key: tuple = (0,)
            when = _esc(str(labels.get("all_day", "All day")))
        else:
            local_start = start.astimezone(LOCAL_TZ)
            key = (1, local_start)
            if end is None or end <= start:
                when = local_start.strftime("%H:%M")  # point event: start only
            else:
                local_end = end.astimezone(LOCAL_TZ)
                when = f"{local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"
        rows.append((key, f'<div><b style="color:{_GOLD_TIME}">{when}</b> · {_esc(title)}</div>'))

    if not rows:
        return ""  # no timeline for an empty day

    # Events outrank free slots for the row budget: the strip exists to show
    # commitments first, breathing room second.
    rows.sort(key=lambda row: row[0])
    rows = rows[:_MAX_ROWS]
    free_label = _esc(str(labels.get("free_slot", "Free")))
    for slot in slots[: _MAX_ROWS - len(rows)]:
        start = slot["start"].astimezone(LOCAL_TZ)
        end = slot["end"].astimezone(LOCAL_TZ)
        text = (
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} · {free_label}"
            f" ({_fmt_minutes(int(slot['minutes']))})"
        )
        rows.append(((1, start), f'<div><i style="color:{_MUTED_GRAY}">{text}</i></div>'))
    rows.sort(key=lambda row: row[0])

    body = "".join(html for _, html in rows)
    return (
        f'<div class="b-timeline" style="border-left:2px solid {_GOLD_BORDER};'
        f'padding-left:12px;margin:0 0 1.1em 0;font-size:0.92em">{body}</div>'
    )
