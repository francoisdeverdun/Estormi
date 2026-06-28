"""Unit tests for ``memory_core.settings.ensure_private_dir`` permissions.

The data dir is the umbrella over every secret and PII file Estormi writes, so
``ensure_private_dir`` must lock it to owner-only ``0o700`` — both when it
creates the dir fresh and when it tightens a pre-existing world-traversable
one. The chmod is best-effort: a filesystem that rejects it must not crash
startup. These tests pin all three behaviours so the hardening can't silently
regress.
"""

from __future__ import annotations

import os
import stat

import pytest

from memory_core import settings

pytestmark = pytest.mark.unit


def _mode(path) -> int:
    """The permission bits (st_mode & 0o777) of ``path``."""
    return stat.S_IMODE(os.stat(path).st_mode)


def test_creates_dir_with_0o700(tmp_path):
    """A freshly created private dir is owner-only (0o700)."""
    target = tmp_path / "data"
    returned = settings.ensure_private_dir(str(target))
    assert returned == str(target)
    assert target.is_dir()
    assert _mode(target) == 0o700


def test_creates_nested_parents_and_locks_leaf(tmp_path):
    """Parents are created (makedirs) and the leaf is locked to 0o700."""
    target = tmp_path / "a" / "b" / "c"
    settings.ensure_private_dir(str(target))
    assert target.is_dir()
    assert _mode(target) == 0o700


def test_tightens_preexisting_world_readable_dir(tmp_path):
    """A pre-existing 0o755 dir is tightened down to 0o700 on the next call."""
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)  # defeat the process umask so the precondition holds
    assert _mode(target) == 0o755

    settings.ensure_private_dir(str(target))
    assert _mode(target) == 0o700


def test_idempotent_on_already_private_dir(tmp_path):
    """Re-running on an already-0o700 dir is a no-op, not an error."""
    target = tmp_path / "data"
    settings.ensure_private_dir(str(target))
    settings.ensure_private_dir(str(target))
    assert _mode(target) == 0o700


def test_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    """A filesystem that rejects chmod must not crash — the OSError is swallowed
    and the dir still exists (creation already succeeded)."""
    target = tmp_path / "data"

    def _boom(*_a, **_k):
        raise OSError("read-only / exotic filesystem rejects chmod")

    # Patch the module's ``os.chmod`` only; ``os.makedirs`` (which runs first)
    # is untouched, so the directory is still created.
    monkeypatch.setattr(settings.os, "chmod", _boom)

    returned = settings.ensure_private_dir(str(target))  # must not raise
    assert returned == str(target)
    assert target.is_dir()
