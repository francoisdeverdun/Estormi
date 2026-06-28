"""Unit tests for ``estormi_ingestion.shared.token_store``.

The token store is the single home for the WHOOP / Google-Calendar OAuth-token
triad: keyring-first, with an atomic chmod-600 file fallback for headless or
locked-keychain hosts. These tests pin the security-sensitive behaviours:

* the file fallback writes 0o600 and round-trips,
* a leftover same-PID ``.tmp`` from a crashed prior run self-heals (O_TRUNC)
  instead of wedging every future save,
* ``delete_token`` removes the fallback file, and
* the keyring-present path stores via keyring and never touches disk.

Keyring is stubbed via ``sys.modules`` — the module does ``import keyring``
inside each function, so swapping the module entry controls which branch runs
without ever touching the real system Keychain.
"""

from __future__ import annotations

import os
import stat
import sys
import types

import pytest

from estormi_ingestion.shared import token_store

pytestmark = pytest.mark.unit

_SERVICE = "estormi-test"
_KEY = "oauth-token"
_TOKEN = {"access_token": "live", "refresh_token": "R1", "expires_at": 0}


def _mode(path) -> int:
    """The permission bits (st_mode & 0o777) of ``path``."""
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def token_file(tmp_path) -> str:
    """A throwaway token-file path inside a not-yet-created subdir.

    The subdir is deliberately absent so the save exercises the
    ``os.makedirs(..., exist_ok=True)`` branch too.
    """
    return str(tmp_path / "secrets" / ".oauth_token")


@pytest.fixture
def no_keyring(monkeypatch):
    """Force the file fallback by making every keyring call raise.

    Mirrors ``test_whoop.py``'s fixture: the module imports ``keyring`` lazily
    inside each function, so a stub in ``sys.modules`` whose functions raise
    pushes ``save_token`` / ``load_token`` onto the file path while still
    proving they tolerate a broken keyring.
    """
    broken = types.ModuleType("keyring")

    def _raise(*_a, **_k):
        raise RuntimeError("keyring disabled in tests")

    broken.set_password = _raise
    broken.get_password = _raise
    broken.delete_password = _raise
    monkeypatch.setitem(sys.modules, "keyring", broken)
    return broken


class _FakeKeyring(types.ModuleType):
    """An in-memory keyring substitute backed by a ``(service, key) -> str`` dict."""

    def __init__(self):
        super().__init__("keyring")
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def get_password(self, service, key):
        return self.store.get((service, key))

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install a working in-memory keyring so the keyring-first path is taken."""
    fk = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fk)
    return fk


# ── File-fallback path ──────────────────────────────────────────────────────


def test_file_fallback_writes_0o600_and_round_trips(no_keyring, token_file):
    """With keyring broken, the token lands in a 0o600 file and reads back."""
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)

    assert os.path.exists(token_file)
    assert _mode(token_file) == 0o600
    # The temp sidecar must not linger after the atomic rename.
    assert not os.path.exists(f"{token_file}.{os.getpid()}.tmp")

    loaded = token_store.load_token(_SERVICE, _KEY, token_file=token_file)
    assert loaded == _TOKEN


def test_save_creates_missing_parent_dir(no_keyring, token_file):
    """The fallback creates the parent dir (exist_ok) rather than erroring."""
    assert not os.path.isdir(os.path.dirname(token_file))
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)
    assert os.path.isdir(os.path.dirname(token_file))


def test_preexisting_same_pid_tmp_self_heals(no_keyring, token_file):
    """A leftover same-PID ``.tmp`` from a crashed prior run does NOT wedge the
    save: O_TRUNC overwrites it, the mode is re-asserted to 0o600, and the new
    token lands intact."""
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    tmp_path = f"{token_file}.{os.getpid()}.tmp"
    # Stale, larger-than-the-new-payload content with a wide-open mode — proves
    # O_TRUNC truncates (no torn tail) and the chmod re-tightens.
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write('{"stale": "leftover-from-a-crashed-run-with-extra-bytes"}')
    os.chmod(tmp_path, 0o644)

    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)

    assert not os.path.exists(tmp_path)
    assert _mode(token_file) == 0o600
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) == _TOKEN


def test_save_overwrites_previous_token(no_keyring, token_file):
    """A second save replaces the first; the file stays 0o600."""
    token_store.save_token(_SERVICE, _KEY, {"access_token": "old"}, token_file=token_file)
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) == _TOKEN
    assert _mode(token_file) == 0o600


def test_load_missing_file_returns_none(no_keyring, token_file):
    """No keyring entry and no file → None, not a crash."""
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) is None


def test_load_malformed_file_returns_none(no_keyring, token_file):
    """A corrupt token file degrades to None rather than raising."""
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) is None


def test_delete_removes_fallback_file(no_keyring, token_file):
    """delete_token removes the on-disk fallback; a later load returns None."""
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)
    assert os.path.exists(token_file)

    token_store.delete_token(_SERVICE, _KEY, token_file=token_file)
    assert not os.path.exists(token_file)
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) is None


def test_delete_missing_file_is_noop(no_keyring, token_file):
    """Deleting when nothing was ever stored must not raise."""
    token_store.delete_token(_SERVICE, _KEY, token_file=token_file)  # no error
    assert not os.path.exists(token_file)


# ── Keyring-present path ──────────────────────────────────────────────────────


def test_keyring_present_stores_via_keyring_not_file(fake_keyring, token_file):
    """When keyring works, the token goes to the keyring and no file is written."""
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)

    import json

    assert json.loads(fake_keyring.store[(_SERVICE, _KEY)]) == _TOKEN
    # The file fallback must be untouched on the happy keyring path.
    assert not os.path.exists(token_file)


def test_keyring_round_trip(fake_keyring, token_file):
    """save_token → load_token round-trips through the keyring substitute."""
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) == _TOKEN


def test_keyring_delete_clears_entry(fake_keyring, token_file):
    """delete_token removes the keyring entry; a later load returns None."""
    token_store.save_token(_SERVICE, _KEY, _TOKEN, token_file=token_file)
    token_store.delete_token(_SERVICE, _KEY, token_file=token_file)
    assert (_SERVICE, _KEY) not in fake_keyring.store
    assert token_store.load_token(_SERVICE, _KEY, token_file=token_file) is None
