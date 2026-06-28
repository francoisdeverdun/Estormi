"""ingest_world.main() — watermark advance/catch-up policy.

These cover the previously-untested decision in main():

  * first run (no watermark) reaches back the full configured window;
  * a subsequent run only catches up the gap since the last success, capped at
    the window;
  * the watermark advances only on a clean (no-failure) run, so a transient
    outage doesn't skip a day's catch-up window;
  * a total failure (all sources failed, nothing ingested) exits non-zero and
    leaves the watermark untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from estormi_ingestion.knowledge import ingest_world as iw

pytestmark = pytest.mark.unit


def _run_main(
    *,
    watermark: str | None,
    window_days: int = 7,
    ingest_return: tuple[int, int, int] = (3, 0, 0),
    raise_in_collect: bool = False,
):
    """Drive iw.main() with mocked sources / ingest / watermark I/O.

    Returns (exit_code, lookback_seen, set_calls). ``lookback_seen`` is the
    ``lookback_days`` main() passed down to _collect_world_items.
    """
    lookback_seen: list[int] = []
    set_calls: list = []

    def _fake_collect(source, *, lookback_days, today):
        lookback_seen.append(lookback_days)
        if raise_in_collect:
            raise RuntimeError("source down")
        return [{"id": "i1"}]

    async def _fake_get_watermark(_key):
        return (watermark, None)

    async def _fake_set_watermark(key, ts):
        set_calls.append((key, ts))

    env = {"KNOWLEDGE_DAYS_WINDOW": str(window_days)}
    with (
        patch.dict("os.environ", env, clear=False),
        patch.object(iw, "_config_path", return_value=_ExistingPath()),
        patch.object(iw, "load_sources", return_value=[{"id": "src1", "kind": "rss"}]),
        patch.object(iw, "_collect_world_items", side_effect=_fake_collect),
        patch.object(iw, "_ingest_items", return_value=ingest_return),
        patch.object(iw, "cleanup_tmp_dir", return_value=None),
        patch.object(iw, "get_watermark", new=_fake_get_watermark),
        patch.object(iw, "set_watermark", new=_fake_set_watermark),
    ):
        exit_code = iw.main()
    return exit_code, lookback_seen, set_calls


class _ExistingPath:
    """Stand-in for the config path whose .exists() is True."""

    def exists(self) -> bool:
        return True


def test_first_run_reaches_back_full_window():
    """No watermark → lookback equals the configured window."""
    exit_code, lookback_seen, set_calls = _run_main(watermark=None, window_days=7)
    assert exit_code == 0
    assert lookback_seen == [7]
    # Clean run → watermark advanced once.
    assert len(set_calls) == 1


def test_catch_up_uses_gap_plus_one_capped_at_window():
    """A 2-day-old watermark → lookback = gap+1 = 3 (< window)."""
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    exit_code, lookback_seen, set_calls = _run_main(watermark=two_days_ago, window_days=7)
    assert exit_code == 0
    assert lookback_seen == [3]
    assert len(set_calls) == 1


def test_catch_up_is_capped_at_window():
    """A 30-day-old watermark with a 7-day window → lookback capped at 7."""
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _, lookback_seen, _ = _run_main(watermark=long_ago, window_days=7)
    assert lookback_seen == [7]


def test_recent_watermark_keeps_minimum_one_day():
    """A watermark from earlier today → gap is 0, lookback floors at 1."""
    earlier_today = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _, lookback_seen, _ = _run_main(watermark=earlier_today, window_days=7)
    assert lookback_seen == [1]


def test_total_failure_does_not_advance_watermark_and_exits_nonzero():
    """Every source raised → nothing ingested → exit 1, watermark untouched."""
    exit_code, _, set_calls = _run_main(watermark=None, raise_in_collect=True)
    assert exit_code == 1
    assert set_calls == [], "a failed run must leave the watermark in place for retry"


def test_partial_failure_does_not_advance_watermark():
    """Some chunks failed (total_failed>0) even with successes → no advance."""
    exit_code, _, set_calls = _run_main(
        watermark=None,
        ingest_return=(2, 0, 1),  # ok=2, skipped=0, failed=1
    )
    # total_ok>0 so exit is 0, but total_failed>0 so the watermark must NOT move.
    assert exit_code == 0
    assert set_calls == [], "a run with any chunk failure must not advance the watermark"
