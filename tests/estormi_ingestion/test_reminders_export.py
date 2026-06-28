"""Unit tests for the date/time handling in the Reminders exporter.

The exporter imports ``EventKit`` at module load and ``sys.exit(1)`` if it is
missing, so the module can't be imported directly off-mac. We stub ``EventKit``
in ``sys.modules`` first, then load the module by path: the functions under test
(``_has_time`` / ``_format_due``) only call methods on the passed-in
``dueDateComponents`` object, so the stub never has to do anything.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_UNDEFINED = 0x7FFFFFFFFFFFFFFF  # NSDateComponentUndefined sentinel


def _load_export_module():
    # Import under the REAL dotted name so coverage attributes the executed lines
    # to packages/estormi_ingestion/reminders/export_reminders.py — loading it via
    # spec_from_file_location under a synthetic name left the real file showing as
    # 0% / "never imported". EventKit is imported at module load (and sys.exit(1)
    # if missing), so stub it for off-mac/CI and remove the stub once loaded,
    # otherwise the empty stub shadows the real framework for later tests (e.g.
    # tests/estormi_ingestion/test_macos_permissions.py reads EKEventStore).
    inserted = "EventKit" not in sys.modules
    if inserted:
        sys.modules["EventKit"] = types.ModuleType("EventKit")
    try:
        return importlib.import_module("estormi_ingestion.reminders.export_reminders")
    finally:
        if inserted:
            del sys.modules["EventKit"]


export = _load_export_module()


class _NSDate:
    """Minimal NSDate stand-in: only ``timeIntervalSince1970`` is used."""

    def __init__(self, epoch: float) -> None:
        self._epoch = epoch

    def timeIntervalSince1970(self) -> float:  # noqa: N802 — Cocoa naming
        return self._epoch


class _Comps:
    """Stub of EKReminder.dueDateComponents()."""

    def __init__(self, y, m, d, hour=None, minute=None, epoch=None):
        self._y, self._m, self._d = y, m, d
        self._hour = _UNDEFINED if hour is None else hour
        self._minute = _UNDEFINED if minute is None else minute
        self._epoch = epoch

    def year(self):  # noqa: N802
        return self._y

    def month(self):  # noqa: N802
        return self._m

    def day(self):  # noqa: N802
        return self._d

    def hour(self):  # noqa: N802
        return self._hour

    def minute(self):  # noqa: N802
        return self._minute

    def date(self):  # noqa: N802
        return _NSDate(self._epoch) if self._epoch is not None else None


def test_has_time_false_for_date_only():
    assert export._has_time(_Comps(2026, 6, 2)) is False


def test_has_time_true_for_timed():
    assert export._has_time(_Comps(2026, 6, 2, hour=9, minute=30)) is True
    # Midnight is still a real time-of-day.
    assert export._has_time(_Comps(2026, 6, 2, hour=0, minute=0)) is True


def test_format_due_date_only_is_bare_local_date():
    """A date-only reminder is a bare YYYY-MM-DD — the repo's all-day encoding,
    same as an all-day calendar event — anchored on the LOCAL day the user picked.

    It must NOT carry a time/offset: a ``…Z`` datetime east of UTC (e.g. Paris)
    rolls the local day back to the previous date, which the briefing then
    mis-announces as "hier soir" / "en retard d'un jour" for a task due today.
    """
    assert export._format_due(_Comps(2026, 6, 2)) == "2026-06-02"


def test_format_due_timed_keeps_time():
    """A timed reminder keeps its exact UTC instant via the NSDate path."""
    epoch = datetime(2026, 6, 2, 7, 30, tzinfo=timezone.utc).timestamp()
    iso = export._format_due(_Comps(2026, 6, 2, hour=9, minute=30, epoch=epoch))
    assert iso == "2026-06-02T07:30:00Z"


def test_format_due_none_is_empty():
    assert export._format_due(None) == ""


@pytest.mark.parametrize("bad", [_Comps(0, 0, 0), _Comps(-1, 6, 2)])
def test_format_due_incomplete_components_fall_back(bad):
    """Incomplete components without a usable NSDate yield "" rather than raising."""
    assert export._format_due(bad) == ""


def test_format_due_incomplete_components_fall_back_to_nsdate():
    """Incomplete components WITH a usable NSDate fall back to that instant."""
    epoch = datetime(2026, 6, 2, 7, 30, tzinfo=timezone.utc).timestamp()
    assert export._format_due(_Comps(0, 0, 0, epoch=epoch)) == "2026-06-02T07:30:00Z"


def test_format_due_timed_with_no_date_is_empty():
    """A timed reminder whose NSDate is missing degrades to "" via _iso_utc(None)."""
    assert export._format_due(_Comps(2026, 6, 2, hour=9, minute=30)) == ""


# --- _iso_utc ---------------------------------------------------------------


def test_iso_utc_none_is_empty():
    assert export._iso_utc(None) == ""


def test_iso_utc_formats_utc():
    epoch = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    assert export._iso_utc(_NSDate(epoch)) == "2026-01-02T03:04:05Z"


def test_iso_utc_swallows_malformed_date():
    """A date object that raises in timeIntervalSince1970 yields "" not a crash."""
    bad = MagicMock()
    bad.timeIntervalSince1970.side_effect = RuntimeError("boom")
    assert export._iso_utc(bad) == ""


# --- _staging_dir -----------------------------------------------------------


def test_staging_dir_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "custom"))
    assert export._staging_dir() == tmp_path / "custom"


def test_staging_dir_strips_whitespace(monkeypatch, tmp_path):
    monkeypatch.setenv("STAGING_DIR", f"  {tmp_path / 'custom'}  ")
    assert export._staging_dir() == tmp_path / "custom"


def test_staging_dir_default_when_unset(monkeypatch):
    monkeypatch.delenv("STAGING_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert export._staging_dir() == Path("/home/tester/estormi-staging/reminders")


def test_staging_dir_default_when_blank(monkeypatch):
    monkeypatch.setenv("STAGING_DIR", "   ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert export._staging_dir() == Path("/home/tester/estormi-staging/reminders")


# --- _safe_id ---------------------------------------------------------------


def test_safe_id_sanitizes_separators():
    assert export._safe_id("a/b\\c:d e") == "a_b_c_d_e"


def test_safe_id_truncates_to_200():
    assert export._safe_id("x" * 500) == "x" * 200


def test_safe_id_plain_passthrough():
    assert export._safe_id("ABC-123_xyz") == "ABC-123_xyz"


# --- _write_atomic ----------------------------------------------------------


def test_write_atomic_writes_content(tmp_path):
    target = tmp_path / "out.txt"
    export._write_atomic(target, b"hello")
    assert target.read_bytes() == b"hello"
    # No stray temp file left behind.
    assert not (tmp_path / ".out.txt.tmp").exists()


def test_write_atomic_overwrites_existing(tmp_path):
    target = tmp_path / "out.txt"
    target.write_bytes(b"old")
    export._write_atomic(target, b"new")
    assert target.read_bytes() == b"new"


# --- _request_access --------------------------------------------------------


def test_request_access_granted():
    store = MagicMock()

    def _invoke(completion):
        completion(True, None)

    store.requestFullAccessToRemindersWithCompletion_.side_effect = _invoke
    assert export._request_access(store) is True


def test_request_access_denied():
    store = MagicMock()

    def _invoke(completion):
        completion(False, "denied")

    store.requestFullAccessToRemindersWithCompletion_.side_effect = _invoke
    assert export._request_access(store) is False


def test_request_access_times_out_returns_false(monkeypatch):
    """If the completion never fires, wait() returns and granted stays False."""
    import threading

    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    store = MagicMock()
    store.requestFullAccessToRemindersWithCompletion_.return_value = None
    assert export._request_access(store) is False


# --- _fetch_reminders -------------------------------------------------------


def test_fetch_reminders_returns_list():
    store = MagicMock()
    sentinel = [object(), object()]

    def _invoke(predicate, completion):
        completion(sentinel)

    store.fetchRemindersMatchingPredicate_completion_.side_effect = _invoke
    assert export._fetch_reminders(store, MagicMock()) == sentinel


def test_fetch_reminders_handles_none():
    store = MagicMock()

    def _invoke(predicate, completion):
        completion(None)

    store.fetchRemindersMatchingPredicate_completion_.side_effect = _invoke
    assert export._fetch_reminders(store, MagicMock()) == []


# --- main -------------------------------------------------------------------


def _make_reminder(*, title, notes, list_name, item_id, comps):
    r = MagicMock()
    r.title.return_value = title
    r.notes.return_value = notes
    r.calendar.return_value.title.return_value = list_name
    r.calendarItemIdentifier.return_value = item_id
    r.dueDateComponents.return_value = comps
    return r


def _patch_eventkit_store(monkeypatch, store):
    fake_ek = types.SimpleNamespace()
    fake_ek.EKEventStore = MagicMock()
    fake_ek.EKEventStore.alloc.return_value.init.return_value = store
    monkeypatch.setattr(export, "EventKit", fake_ek)


def test_main_access_denied_returns_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STAGING_DIR", str(tmp_path))
    store = MagicMock()
    store.requestFullAccessToRemindersWithCompletion_.side_effect = lambda cb: cb(False, None)
    _patch_eventkit_store(monkeypatch, store)
    assert export.main() == 1
    assert "denied" in capsys.readouterr().err


def test_main_exports_reminders_and_writes_flag(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STAGING_DIR", str(tmp_path))
    store = MagicMock()
    store.requestFullAccessToRemindersWithCompletion_.side_effect = lambda cb: cb(True, None)
    epoch = datetime(2026, 6, 2, 7, 30, tzinfo=timezone.utc).timestamp()
    reminders = [
        _make_reminder(
            title="Buy milk",
            notes="2%",
            list_name="Errands",
            item_id="id/one",
            comps=_Comps(2026, 6, 2, hour=9, minute=30, epoch=epoch),
        ),
        _make_reminder(
            title="No due",
            notes="",
            list_name="Inbox",
            item_id="id two",
            comps=None,
        ),
    ]
    store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda pred, cb: cb(reminders)
    _patch_eventkit_store(monkeypatch, store)

    assert export.main() == 0

    # First reminder: id sanitized id/one -> id_one
    body1 = (tmp_path / "id_one.txt").read_text()
    assert "List: Errands" in body1
    assert "Title: Buy milk" in body1
    assert "Due: 2026-06-02T07:30:00Z" in body1
    assert "Status: pending" in body1
    assert "Notes: 2%" in body1
    meta1 = json.loads((tmp_path / "id_one.meta.json").read_text())
    assert meta1 == {
        "title": "Buy milk",
        "date": "2026-06-02T07:30:00Z",
        "list": "Errands",
        "completed": False,
        "id": "id_one",
    }

    # Second reminder: no due date -> no Due line, no Notes line.
    body2 = (tmp_path / "id_two.txt").read_text()
    assert "Due:" not in body2
    assert "Notes:" not in body2
    meta2 = json.loads((tmp_path / "id_two.meta.json").read_text())
    assert meta2["date"] == ""

    # Completeness flag written, both exported.
    assert (tmp_path / "_export_complete.flag").exists()
    assert "2 reminders exported" in capsys.readouterr().out


def test_main_clears_stale_flag_before_run(monkeypatch, tmp_path):
    monkeypatch.setenv("STAGING_DIR", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    stale = tmp_path / "_export_complete.flag"
    stale.write_bytes(b"stale")
    store = MagicMock()
    store.requestFullAccessToRemindersWithCompletion_.side_effect = lambda cb: cb(True, None)
    store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda pred, cb: cb([])
    _patch_eventkit_store(monkeypatch, store)

    assert export.main() == 0
    # Empty run still rewrites the flag (no failures), but it must be the fresh
    # empty one, not the stale content.
    assert stale.read_bytes() == b""


def test_main_write_failure_skips_flag(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STAGING_DIR", str(tmp_path))
    store = MagicMock()
    store.requestFullAccessToRemindersWithCompletion_.side_effect = lambda cb: cb(True, None)
    reminders = [
        _make_reminder(
            title="T",
            notes="",
            list_name="L",
            item_id="boom",
            comps=None,
        )
    ]
    store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda pred, cb: cb(reminders)
    _patch_eventkit_store(monkeypatch, store)

    real_write = export._write_atomic

    def _failing_write(path, data):
        if path.name.endswith(".txt"):
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(export, "_write_atomic", _failing_write)

    assert export.main() == 0
    err = capsys.readouterr().err
    assert "failed to write body" in err
    # Body write failed, so the flag must NOT be written.
    assert not (tmp_path / "_export_complete.flag").exists()


def test_main_meta_write_failure_skips_flag(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STAGING_DIR", str(tmp_path))
    store = MagicMock()
    store.requestFullAccessToRemindersWithCompletion_.side_effect = lambda cb: cb(True, None)
    reminders = [
        _make_reminder(
            title="T",
            notes="",
            list_name="L",
            item_id="boom",
            comps=None,
        )
    ]
    store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda pred, cb: cb(reminders)
    _patch_eventkit_store(monkeypatch, store)

    real_write = export._write_atomic

    def _failing_meta(path, data):
        if path.name.endswith(".meta.json"):
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(export, "_write_atomic", _failing_meta)

    assert export.main() == 0
    err = capsys.readouterr().err
    assert "failed to write meta" in err
    assert not (tmp_path / "_export_complete.flag").exists()
    # Body was written before the meta failure.
    assert (tmp_path / "boom.txt").exists()
