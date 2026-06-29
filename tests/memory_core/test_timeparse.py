"""memory_core.timeparse — local-day windowing, tz resolution, ISO parsing.

These pin the load-bearing time contract behind the briefing's day bucketing:
``resolve_local_tz`` (the single source of "the user's day", shared by the
server and the Briefing engine) and ``local_day_window`` (anchors a window on
local-day edges so a non-UTC user neither leaks tomorrow nor loses today's
evening — the root fix for the "briefing mixes current & next day" report).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from memory_core.timeparse import local_day_window, parse_iso, resolve_local_tz

pytestmark = pytest.mark.unit


def test_resolve_local_tz_honours_env(monkeypatch):
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "Asia/Tokyo")
    assert resolve_local_tz() == ZoneInfo("Asia/Tokyo")


def test_resolve_local_tz_invalid_falls_back_without_raising(monkeypatch):
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "Not/AZone")
    tz = resolve_local_tz()  # must not raise
    assert tz is not None


def test_resolve_local_tz_read_fresh_each_call(monkeypatch):
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "Asia/Tokyo")
    assert resolve_local_tz() == ZoneInfo("Asia/Tokyo")
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "America/New_York")
    assert resolve_local_tz() == ZoneInfo("America/New_York")  # no import-time freeze


def test_local_day_window_anchors_east_of_utc(monkeypatch):
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "Europe/Paris")  # CEST = +02:00 in June
    lo, hi = local_day_window(date(2026, 6, 14), window_days=0, forward_days=0)
    # Local midnight Paris 2026-06-14 == 2026-06-13T22:00Z; next local day == 06-14T22:00Z.
    assert parse_iso(lo) == datetime(2026, 6, 13, 22, 0, tzinfo=timezone.utc)
    assert parse_iso(hi) == datetime(2026, 6, 14, 22, 0, tzinfo=timezone.utc)
    assert (parse_iso(hi) - parse_iso(lo)).total_seconds() == 24 * 3600  # exactly one day


def test_local_day_window_anchors_west_of_utc(monkeypatch):
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "America/New_York")  # EDT = -04:00 in June
    lo, hi = local_day_window(date(2026, 6, 14), window_days=0, forward_days=0)
    assert parse_iso(lo) == datetime(2026, 6, 14, 4, 0, tzinfo=timezone.utc)
    assert parse_iso(hi) == datetime(2026, 6, 15, 4, 0, tzinfo=timezone.utc)


def test_local_day_window_lookback_only_and_symmetric_default(monkeypatch):
    monkeypatch.setenv("ESTORMI_LOCAL_TZ", "UTC")
    # window_days=2, forward_days=0 → the day plus the prior 2 days, no tomorrow.
    lo, hi = local_day_window(date(2026, 6, 14), window_days=2, forward_days=0)
    assert parse_iso(lo) == datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_iso(hi) == datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
    # forward_days=None keeps the look-ahead symmetric with the look-back.
    lo2, hi2 = local_day_window(date(2026, 6, 14), window_days=1)
    assert parse_iso(lo2) == datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc)
    assert parse_iso(hi2) == datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_normalises_z_naive_and_offset():
    utc = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    assert parse_iso("2026-06-14T12:00:00Z") == utc  # trailing Z
    assert parse_iso("2026-06-14T12:00:00+00:00") == utc  # explicit UTC
    assert parse_iso("2026-06-14T12:00:00") == utc  # naive assumed UTC
    assert parse_iso("2026-06-14T14:00:00+02:00") == utc  # offset → real instant


def test_parse_iso_returns_none_on_garbage():
    for bad in (None, "", "not-a-date", "2026-13-99"):
        assert parse_iso(bad) is None
