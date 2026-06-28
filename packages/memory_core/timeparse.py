"""Shared ISO-8601 parsing and formatting for the memory layer.

A single best-effort parser plus the two "now as ISO-8601 UTC" formatters used
across the server, the briefing engine, and DAG-run state — previously
duplicated as byte-identical private helpers. Lives in :mod:`memory_core` (the
bottom layer) so every higher package can import it without violating the
one-way dependency direction.
"""

from __future__ import annotations

import logging
import os
from datetime import date as _date
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "parse_iso",
    "now_iso",
    "now_iso_z",
    "resolve_local_tz",
    "local_day_window",
]


def resolve_local_tz() -> tzinfo:
    """Local timezone for bucketing time into the user's "days".

    Honors the ``ESTORMI_LOCAL_TZ`` override (an IANA name, e.g.
    ``America/New_York``); otherwise derives the machine's local timezone from
    the system clock so no region is hardcoded. Read at call time so a test (or
    a relocated machine) can set the env without an import-time freeze.

    The single source of this resolution: the Briefing's ``day._resolve_local_tz``
    delegates here, and the server's :func:`fetch_around` uses it to anchor a
    bare-date window on the same local day the briefing buckets into — keeping
    the briefing and the server's ``fetch_around`` in agreement on "today"
    without ``estormi_server`` importing ``estormi_briefing`` (this lives in the
    bottom layer, so both can).
    """
    override = os.getenv("ESTORMI_LOCAL_TZ", "").strip()
    if override:
        try:
            return ZoneInfo(override)
        except (ZoneInfoNotFoundError, ValueError):
            logging.getLogger(__name__).warning(
                "Ignoring invalid ESTORMI_LOCAL_TZ=%r; using system timezone", override
            )
    return datetime.now().astimezone().tzinfo or timezone.utc


def local_day_window(
    day: _date, window_days: int, forward_days: int | None = None
) -> tuple[str, str]:
    """UTC ISO ``(lo, hi)`` bounds for a window of LOCAL days centred on ``day``.

    ``lo`` is the start of the local day ``day - window_days``; ``hi`` is the
    start of the local day ``day + forward_days + 1`` — i.e. the window is
    inclusive of its last local day (``+1`` so a same-day, ``forward_days=0``
    window still spans the whole of ``day`` rather than collapsing to a single
    instant), mirroring the day-granular ``+1`` the symmetric UTC path uses.
    ``forward_days=None`` keeps the look-ahead symmetric with the look-back.

    Both bounds are emitted as ``…+00:00`` UTC instants, so a downstream
    ``datetime(date_ts) <= datetime(hi)`` overlap test compares real instants —
    the local-day edges land at the correct UTC offset (e.g. local midnight in
    Paris is the prior ``22:00Z``), so an east-of-UTC user no longer leaks
    tomorrow's early morning and a west-of-UTC user no longer loses today's
    evening.
    """
    tz = resolve_local_tz()
    fwd = window_days if forward_days is None else forward_days
    lo_day = day - timedelta(days=window_days)
    hi_day = day + timedelta(days=fwd + 1)
    lo = datetime.combine(lo_day, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
    hi = datetime.combine(hi_day, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
    return lo, hi


def parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 → timezone-aware ``datetime``.

    Accepts a trailing ``Z`` (normalized to ``+00:00``) and assumes UTC for a
    naive timestamp. Returns ``None`` for empty input or anything unparseable.
    """
    if not value:
        return None
    try:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# The two formatters below are intentionally distinct — each format is part of
# an existing on-disk contract, so neither may change:
#
# * :func:`now_iso` — full-precision ``+00:00`` offset form, stored in SQLite
#   rows (engine-lock and DAG-run state).
# * :func:`now_iso_z` — second-precision ``Z``-suffixed form, written into the
#   vault JSON payloads the iOS companion parses.
#
# Both round-trip through :func:`parse_iso`.


def now_iso() -> str:
    """Current UTC time, full precision with explicit offset.

    Example: ``2026-06-10T12:34:56.789012+00:00``.
    """
    return datetime.now(timezone.utc).isoformat()


def now_iso_z() -> str:
    """Current UTC time, second precision with a ``Z`` suffix.

    Example: ``2026-06-10T12:34:56Z``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
