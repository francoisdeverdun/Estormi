"""Relocatable data-dir resolution + crash-safe library relocation.

These exercise ``memory_core.datadir`` directly. The suite pins
``ESTORMI_DATA_DIR``/``ESTORMI_CONFIG_HOME`` globally (see ``tests/conftest.py``),
so each test clears or repoints them via ``monkeypatch`` to drive the
env → pointer → default precedence and the relocation state machine.
"""

from __future__ import annotations

import sqlite3

import pytest

from memory_core import datadir

pytestmark = pytest.mark.unit


def _make_db(path) -> None:
    """A minimal but valid SQLite file so ``PRAGMA integrity_check`` returns ok."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Repoint the fixed config home (pointer + marker live here) at a tmp dir
    and clear the env pin so the pointer/default path is exercised."""
    home = tmp_path / "cfg"
    home.mkdir()
    monkeypatch.setenv("ESTORMI_CONFIG_HOME", str(home))
    monkeypatch.delenv("ESTORMI_DATA_DIR", raising=False)
    return home


class TestResolvePrecedence:
    def test_env_wins(self, cfg_home, tmp_path, monkeypatch):
        monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path / "envdir"))
        datadir.write_relocation_marker(str(tmp_path / "a"), str(tmp_path / "b"))
        datadir._write_pointer(str(tmp_path / "pointed"))
        assert datadir.resolve_data_dir() == str(tmp_path / "envdir")

    def test_pointer_used_when_no_env(self, cfg_home, tmp_path):
        target = tmp_path / "lib"
        target.mkdir()
        datadir._write_pointer(str(target))
        assert datadir.resolve_data_dir() == str(target)

    def test_default_when_nothing_set(self, cfg_home):
        assert datadir.resolve_data_dir() == datadir.default_data_dir() == str(cfg_home)

    def test_pointer_ignored_when_volume_missing(self, cfg_home, tmp_path, monkeypatch):
        datadir._write_pointer(str(tmp_path / "ghost"))
        monkeypatch.setattr(datadir, "_volume_ready", lambda _p: False)
        assert datadir.resolve_data_dir() == datadir.default_data_dir()


class TestMarker:
    def test_write_and_read_pending(self, cfg_home, tmp_path):
        datadir.write_relocation_marker(str(tmp_path / "from"), str(tmp_path / "to"))
        assert datadir.pending_relocation() == str(tmp_path / "to")

    def test_no_pending_when_absent(self, cfg_home):
        assert datadir.pending_relocation() is None


class TestBootstrapRelocate:
    def test_noop_when_env_pinned(self, cfg_home, tmp_path, monkeypatch):
        monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path / "env"))
        datadir.write_relocation_marker(str(tmp_path / "a"), str(tmp_path / "b"))
        assert datadir.bootstrap_relocate() is None
        # Marker untouched (the env pin owns the path).
        assert datadir.pending_relocation() == str(tmp_path / "b")

    def test_noop_when_no_marker(self, cfg_home):
        assert datadir.bootstrap_relocate() is None

    def test_corrupt_marker_is_cleared(self, cfg_home):
        with open(datadir.marker_path(), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert datadir.bootstrap_relocate() is None
        assert datadir.pending_relocation() is None

    def test_happy_path_copies_verifies_flips_and_backs_up(self, cfg_home, tmp_path):
        src = tmp_path / "old"
        dst = tmp_path / "new"
        _make_db(src / "estormi.db")
        (src / "models").mkdir()
        (src / "models" / "x.bin").write_text("payload")

        datadir.write_relocation_marker(str(src), str(dst))
        result = datadir.bootstrap_relocate()

        assert result == str(dst)
        assert (dst / "estormi.db").exists()
        assert (dst / "models" / "x.bin").read_text() == "payload"
        assert datadir.read_pointer() == str(dst)
        assert datadir.pending_relocation() is None
        # src wasn't the default → kept as a timestamped backup.
        backups = list(tmp_path.glob("old.migrated-*"))
        assert len(backups) == 1
        assert not src.exists()

    def test_default_source_left_in_place_as_backup(self, cfg_home, tmp_path):
        # When the source IS the config home (holds the pointer), it is not
        # renamed away — its data stays as the backup.
        _make_db(cfg_home / "estormi.db")
        dst = tmp_path / "new"
        datadir.write_relocation_marker(str(cfg_home), str(dst))
        assert datadir.bootstrap_relocate() == str(dst)
        assert cfg_home.exists()  # still there (pointer lives in it)
        assert (cfg_home / "estormi.db").exists()  # data left as backup
        # The relocation bookkeeping is not carried into the moved library.
        assert not (dst / ".relocate-pending.json").exists()
        assert datadir.read_pointer() == str(dst)

    def test_idempotent_resume_when_dst_db_present(self, cfg_home, tmp_path):
        # Crash after copy, before flipping the pointer: dst DB already exists.
        src = tmp_path / "old"
        dst = tmp_path / "new"
        _make_db(src / "estormi.db")
        _make_db(dst / "estormi.db")  # pretend the copy already happened
        datadir.write_relocation_marker(str(src), str(dst))
        assert datadir.bootstrap_relocate() == str(dst)
        assert datadir.read_pointer() == str(dst)
        assert datadir.pending_relocation() is None
        assert src.exists()  # not re-copied / not backed up on resume

    def test_fresh_install_adopts_empty_dst(self, cfg_home, tmp_path):
        # No source DB yet (fresh machine): adopt the destination, no copy.
        src = tmp_path / "old"  # never created
        dst = tmp_path / "new"
        datadir.write_relocation_marker(str(src), str(dst))
        assert datadir.bootstrap_relocate() == str(dst)
        assert dst.exists()
        assert datadir.read_pointer() == str(dst)

    def test_target_unavailable_keeps_marker(self, cfg_home, tmp_path, monkeypatch):
        src = tmp_path / "old"
        dst = tmp_path / "new"
        _make_db(src / "estormi.db")
        datadir.write_relocation_marker(str(src), str(dst))
        monkeypatch.setattr(datadir, "_volume_ready", lambda _p: False)
        assert datadir.bootstrap_relocate() is None
        assert datadir.pending_relocation() == str(dst)  # retried next launch
        assert datadir.read_pointer() is None  # not flipped

    def test_integrity_failure_aborts_flip(self, cfg_home, tmp_path):
        src = tmp_path / "old"
        dst = tmp_path / "new"
        _make_db(src / "estormi.db")
        datadir.write_relocation_marker(str(src), str(dst))
        # Corrupt the copy's DB during the move so integrity_check fails.
        orig_copytree = datadir.shutil.copytree

        def _corrupting_copytree(s, d, **kw):
            orig_copytree(s, d, **kw)
            with open(f"{d}/estormi.db", "wb") as fh:
                fh.write(b"not a database")

        import pytest as _pytest  # local alias to avoid shadowing

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(datadir.shutil, "copytree", _corrupting_copytree)
            assert datadir.bootstrap_relocate() is None
        assert datadir.read_pointer() is None  # flip aborted
        assert datadir.pending_relocation() == str(dst)  # marker kept (visible failure)
