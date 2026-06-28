"""Unit tests for macOS Contacts → phone-number name resolution.

The module reads the address book through the native Contacts framework
(``CNContactStore`` via PyObjC). Tests mock ``_contacts_pairs`` — the single
seam that returns ``[(name, phone), …]`` — so the index-building, suffix
matching and cache logic are exercised without a real Contacts grant or the
framework being present (CI / Linux).
"""

from __future__ import annotations

import importlib
import os
import stat

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def macos_contacts(monkeypatch, tmp_path):
    """Import the module fresh with an isolated on-disk cache per test."""
    import estormi_server.integrations.macos_contacts as mc

    importlib.reload(mc)
    # Redirect the disk cache into a temp dir so tests don't touch the real one.
    monkeypatch.setattr(mc, "_DISK_CACHE_PATH", tmp_path / "contacts_index.json")
    monkeypatch.setattr(mc, "_cache", None)
    return mc


@pytest.fixture
def macos_contacts_data_dir(monkeypatch, tmp_path):
    """Module reloaded with ``ESTORMI_DATA_DIR`` pointed at a fresh tmp dir.

    The module resolves ``_DATA_DIR`` / ``_DISK_CACHE_PATH`` from the env at
    import time, so set the env *then* reload — that way ``_save_disk_cache``
    creates the dir and writes ``contacts_index.json`` under ``tmp_path``,
    exercising the real on-disk persistence (and its 0o600 hardening) rather
    than a monkeypatched path. Yields ``(module, data_dir)``.
    """
    data_dir = tmp_path / "Estormi"
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(data_dir))

    import estormi_server.integrations.macos_contacts as mc

    importlib.reload(mc)
    monkeypatch.setattr(mc, "_cache", None)
    assert mc._DISK_CACHE_PATH == data_dir / "contacts_index.json"
    return mc, data_dir


def test_suffix_normalisation(macos_contacts):
    mc = macos_contacts
    # Different national formats collapse to the same 9-digit suffix.
    assert mc._suffix("+33 6 12 34 56 78") == mc._suffix("06 12 34 56 78")
    assert mc._suffix("33612345678@s.whatsapp.net") == mc._suffix("0612345678")
    # Too few digits → kept whole (no false matches on short codes).
    assert mc._suffix("123") == "123"


def test_index_built_from_contacts(macos_contacts, monkeypatch):
    mc = macos_contacts
    monkeypatch.setattr(
        mc,
        "_contacts_pairs",
        lambda: [("Alice Martin", "+33 6 12 34 56 78"), ("Bob Durand", "06 98 76 54 32")],
    )
    idx = mc.phone_name_index(force_refresh=True)
    assert idx[mc._suffix("+33 6 12 34 56 78")] == "Alice Martin"
    assert idx[mc._suffix("0033698765432")] == "Bob Durand"


def test_ambiguous_suffix_dropped(macos_contacts, monkeypatch):
    mc = macos_contacts
    # Two different names sharing a suffix → dropped (better no label than wrong).
    monkeypatch.setattr(
        mc,
        "_contacts_pairs",
        lambda: [("X", "0612345678"), ("Y", "0612345678")],
    )
    idx = mc.phone_name_index(force_refresh=True)
    assert idx == {}


def test_same_name_repeated_suffix_kept(macos_contacts, monkeypatch):
    mc = macos_contacts
    # Same person listed twice (e.g. home + mobile dupes) is NOT ambiguous.
    monkeypatch.setattr(
        mc,
        "_contacts_pairs",
        lambda: [("Dana", "0612345678"), ("Dana", "+33 6 12 34 56 78")],
    )
    idx = mc.phone_name_index(force_refresh=True)
    assert idx[mc._suffix("0612345678")] == "Dana"


def test_name_for_phone_lookup(macos_contacts):
    mc = macos_contacts
    idx = {mc._suffix("0612345678"): "Charlie"}
    assert mc.name_for_phone("33612345678@s.whatsapp.net", idx) == "Charlie"


def test_name_for_phone_unknown(macos_contacts):
    mc = macos_contacts
    assert (
        mc.name_for_phone("33600000000@s.whatsapp.net", {mc._suffix("0612345678"): "Charlie"})
        is None
    )


def test_unavailable_returns_empty(macos_contacts, monkeypatch):
    mc = macos_contacts
    # No PyObjC Contacts framework (non-macOS / sandbox) → empty index, no raise.
    monkeypatch.setattr(mc, "_contacts_module", lambda: None)
    assert mc.phone_name_index(force_refresh=True) == {}


def test_denied_returns_empty(macos_contacts, monkeypatch):
    mc = macos_contacts
    # Access denied → no enumeration, empty index, no prompt loop.
    monkeypatch.setattr(mc, "contacts_authorization_status", lambda: "denied")
    monkeypatch.setattr(mc, "_contacts_module", lambda: object())
    assert mc.phone_name_index(force_refresh=True) == {}


# ── On-disk cache: permission hardening + round-trip ──────────────────────────


def test_disk_cache_written_0o600(macos_contacts_data_dir, monkeypatch):
    """Building a non-empty index persists it to disk as an owner-only (0o600)
    file — the index maps phone suffixes to third-party contact names (PII) and
    must never be world-readable on a multi-user box."""
    mc, data_dir = macos_contacts_data_dir
    monkeypatch.setattr(mc, "_contacts_pairs", lambda: [("Alice Martin", "+33 6 12 34 56 78")])

    mc.phone_name_index(force_refresh=True)

    cache = data_dir / "contacts_index.json"
    assert cache.exists()
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    # The atomic write's tmp sidecar is renamed away, not left behind.
    assert not cache.with_suffix(cache.suffix + ".tmp").exists()


def test_disk_cache_round_trips_via_load(macos_contacts_data_dir, monkeypatch):
    """A persisted index is read back by ``_load_disk_cache`` (fresh, within
    TTL) and reused by ``phone_name_index`` without re-enumerating contacts."""
    mc, _ = macos_contacts_data_dir
    monkeypatch.setattr(mc, "_contacts_pairs", lambda: [("Bob Durand", "06 98 76 54 32")])

    built = mc.phone_name_index(force_refresh=True)
    assert built[mc._suffix("0033698765432")] == "Bob Durand"

    # Drop the in-memory cache; the disk cache alone must satisfy the next read,
    # and _contacts_pairs must NOT be called again (would raise if it were).
    monkeypatch.setattr(mc, "_cache", None)

    def _must_not_enumerate():
        raise AssertionError("disk cache should serve the read; no re-enumeration")

    monkeypatch.setattr(mc, "_contacts_pairs", _must_not_enumerate)

    loaded = mc._load_disk_cache()
    assert loaded is not None
    age, index = loaded
    assert age >= 0.0
    assert index == built
    # phone_name_index reuses the on-disk cache rather than rebuilding.
    assert mc.phone_name_index() == built


def test_load_disk_cache_malformed_returns_none(macos_contacts_data_dir):
    """A corrupt cache file degrades to ``None`` (forcing a fresh build) rather
    than crashing the Settings-UI poll."""
    mc, data_dir = macos_contacts_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "contacts_index.json").write_text("{ this is not valid json")

    assert mc._load_disk_cache() is None


def test_load_disk_cache_wrong_typed_index_returns_none(macos_contacts_data_dir):
    """Valid JSON but the ``index`` is not a dict → ``None``, no crash."""
    mc, data_dir = macos_contacts_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    import json

    (data_dir / "contacts_index.json").write_text(
        json.dumps({"built_at_wall": 1.0, "index": ["not", "a", "dict"]})
    )

    assert mc._load_disk_cache() is None


def test_load_disk_cache_missing_file_returns_none(macos_contacts_data_dir):
    """No cache file yet → ``None`` (cold start), not an exception."""
    mc, _ = macos_contacts_data_dir
    assert mc._load_disk_cache() is None


def test_empty_index_not_persisted(macos_contacts_data_dir, monkeypatch):
    """An empty build (denied/unavailable Contacts) is NOT written to disk —
    only a real, non-empty index earns the long-TTL persistence."""
    mc, data_dir = macos_contacts_data_dir
    monkeypatch.setattr(mc, "_contacts_pairs", lambda: [])

    assert mc.phone_name_index(force_refresh=True) == {}
    assert not (data_dir / "contacts_index.json").exists()
