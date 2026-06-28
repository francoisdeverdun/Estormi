"""Tests for memory_core/engine_lock.py — the DB-backed advisory engine lock.

Covers the acquire/held/steal-if-dead state machine, owner-scoped release,
cross-engine mutual exclusion, the cross-host guard, force_release, and the CLI
exit codes the shell script relies on.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from memory_core import engine_lock

pytestmark = pytest.mark.unit


@pytest.fixture
def lock_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "estormi-engine-lock.db")
    monkeypatch.setattr(engine_lock, "DB_PATH_OVERRIDE", db_path)
    # Each tmp path is fresh; drop the per-path schema-applied memo so the
    # connection re-applies the DDL against this DB.
    monkeypatch.setattr(engine_lock, "_SCHEMA_APPLIED_PATHS", set())
    return db_path


def _dead_pid() -> int:
    """A pid (== pgid, own session) that is guaranteed dead: spawned then reaped."""
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    p.wait()
    return p.pid


class TestAcquireRelease:
    def test_acquire_on_empty_slot(self, lock_db):
        assert engine_lock.acquire("ingestion", os.getpid(), os.getpgrp()) == "acquired"
        row = engine_lock.current()
        assert row is not None
        assert row.kind == "ingestion"
        assert row.pid == os.getpid()

    def test_second_acquire_while_live_is_held(self, lock_db):
        assert engine_lock.acquire("ingestion", os.getpid(), os.getpgrp()) == "acquired"
        # Cross-engine: a *different* kind is refused too — one engine at a time.
        assert engine_lock.acquire("briefing", os.getpid(), os.getpgrp()) == "held"
        assert engine_lock.current().kind == "ingestion"

    def test_release_frees_slot(self, lock_db):
        engine_lock.acquire("briefing", os.getpid(), os.getpgrp())
        assert engine_lock.release("briefing", os.getpid()) is True
        assert engine_lock.current() is None
        # A fresh acquire now succeeds.
        assert engine_lock.acquire("ingestion", os.getpid(), os.getpgrp()) == "acquired"

    def test_release_only_by_owner(self, lock_db):
        engine_lock.acquire("ingestion", os.getpid(), os.getpgrp())
        # Wrong pid → no-op, lock untouched.
        assert engine_lock.release("ingestion", os.getpid() + 1) is False
        # Wrong kind → no-op too.
        assert engine_lock.release("briefing", os.getpid()) is False
        assert engine_lock.current().kind == "ingestion"


class TestStealIfDead:
    def test_dead_owner_is_stolen(self, lock_db):
        dead = _dead_pid()
        assert engine_lock.acquire("ingestion", dead, dead) == "acquired"
        assert not engine_lock.is_alive(engine_lock.current())
        # A new engine reclaims the slot from the dead owner.
        assert engine_lock.acquire("briefing", os.getpid(), os.getpgrp()) == "acquired"
        assert engine_lock.current().kind == "briefing"

    def test_live_owner_not_stolen(self, lock_db):
        engine_lock.acquire("ingestion", os.getpid(), os.getpgrp())
        assert engine_lock.acquire("briefing", os.getpid(), os.getpgrp()) == "held"


class TestIsAlive:
    def test_self_is_alive(self, lock_db):
        engine_lock.acquire("ingestion", os.getpid(), os.getpgrp())
        assert engine_lock.is_alive(engine_lock.current()) is True

    def test_dead_is_not_alive(self, lock_db):
        dead = _dead_pid()
        engine_lock.acquire("ingestion", dead, dead)
        assert engine_lock.is_alive(engine_lock.current()) is False

    def test_none_is_not_alive(self):
        assert engine_lock.is_alive(None) is False

    def test_cross_host_never_stolen(self, lock_db):
        # A holder on another machine can't be probed, so it reads as alive and
        # is never stolen — even with a pid that's dead on *this* host.
        engine_lock.acquire("ingestion", _dead_pid(), source=None, host="some-other-mac.local")
        row = engine_lock.current()
        assert engine_lock.is_alive(row) is True
        assert engine_lock.acquire("briefing", os.getpid(), os.getpgrp()) == "held"


class TestForceRelease:
    def test_force_release_drops_any_holder(self, lock_db):
        engine_lock.acquire("ingestion", os.getpid(), os.getpgrp())
        assert engine_lock.force_release() is True
        assert engine_lock.current() is None
        assert engine_lock.force_release() is False  # idempotent — nothing left


class TestCLI:
    def test_acquire_then_held_exit_codes(self, lock_db):
        assert engine_lock._main(["acquire", "--kind", "ingestion", "--pid", str(os.getpid())]) == 0
        # Second acquire by a live owner → exit 1 so the shell script refuses.
        assert engine_lock._main(["acquire", "--kind", "briefing", "--pid", str(os.getpid())]) == 1

    def test_release_cli(self, lock_db):
        engine_lock._main(["acquire", "--kind", "ingestion", "--pid", str(os.getpid())])
        assert engine_lock._main(["release", "--kind", "ingestion", "--pid", str(os.getpid())]) == 0
        assert engine_lock.current() is None

    def test_status_cli(self, lock_db, capsys):
        engine_lock._main(["status"])
        assert "idle" in capsys.readouterr().out
        engine_lock.acquire("briefing", os.getpid(), os.getpgrp())
        engine_lock._main(["status"])
        assert "briefing" in capsys.readouterr().out
