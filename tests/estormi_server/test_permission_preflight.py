"""Tests for the permission preflight — probe + persist, grouped at launch.

The preflight is the single place that probes macOS TCC; it persists each
verified status so the run-time gate and the UI can read it without re-probing.
See ``estormi_server/server/permission_preflight.py``.
"""

from __future__ import annotations

import pytest

# Every test here drives the wired_tools_db fixture (real SQLite chunk store),
# so this crosses the storage boundary — it is an integration test, not a unit.
pytestmark = pytest.mark.integration

_AUTHORIZED = {
    "key": "Reminders",
    "label": "Reminders access",
    "status": "authorized",
    "detail": "granted",
    "settings_pane": None,
}


async def test_preflight_probes_only_enabled_sources_and_persists(wired_tools_db, monkeypatch):
    from estormi_server.server import permission_preflight as pf
    from estormi_server.server import permissions as perms
    from estormi_server.storage import tools

    db = tools._db
    # reminders enabled (needs a permission); notes disabled (must be skipped).
    await db.execute("INSERT INTO settings(key,value) VALUES('source_reminders_enabled','true')")
    await db.execute("INSERT INTO settings(key,value) VALUES('source_notes_enabled','false')")
    await db.commit()

    probed: list[str] = []

    def _fake_ensure(name, root=None):
        probed.append(name)
        return dict(_AUTHORIZED)

    monkeypatch.setattr(perms, "ensure_source_permission", _fake_ensure)
    monkeypatch.setattr(perms, "probe_working_set_volumes", lambda extra=(): [])

    result = await pf.run_preflight()

    assert probed == ["reminders"]  # disabled / permissionless sources untouched
    assert result["sources"] == [_AUTHORIZED]
    assert result["volumes"] == []

    import json

    async with db.execute(
        "SELECT value FROM settings WHERE key = 'source_reminders_permission'"
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row[0])["status"] == "authorized"


async def test_preflight_persists_removable_volumes(wired_tools_db, monkeypatch):
    from estormi_server.server import permission_preflight as pf
    from estormi_server.server import permissions as perms
    from estormi_server.storage import tools

    db = tools._db
    vol = {
        "key": "RemovableVolume:/Volumes/SSD",
        "label": "Removable volume (SSD)",
        "status": "authorized",
        "detail": "granted",
        "settings_pane": None,
    }
    monkeypatch.setattr(perms, "ensure_source_permission", lambda name, root=None: None)
    monkeypatch.setattr(perms, "probe_working_set_volumes", lambda extra=(): [vol])

    result = await pf.run_preflight()
    assert result["volumes"] == [vol]

    import json

    async with db.execute(
        "SELECT value FROM settings WHERE key = ?", (f"volume_permission_{vol['key']}",)
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row[0]) == vol


async def test_reprobe_refreshes_permission_when_root_changes(wired_tools_db, monkeypatch):
    """A folder-rooted source's root arrives via PUT /api/settings (the folder
    picker), not the toggle — reprobe must re-run the probe against the new root
    and persist ground truth so the run gate stops reading the stale status."""
    from estormi_server.server import permission_preflight as pf
    from estormi_server.server import permissions as perms
    from estormi_server.storage import tools

    db = tools._db
    await db.execute("INSERT INTO settings(key,value) VALUES('source_documents_enabled','true')")
    await db.execute("INSERT INTO settings(key,value) VALUES('documents_root','/tmp/docs')")
    await db.commit()

    seen: dict = {}

    def _fake_ensure(name, root=None):
        seen["name"], seen["root"] = name, root
        return {"key": "FilesAndFolders", "status": "authorized", "settings_pane": None}

    monkeypatch.setattr(perms, "ensure_source_permission", _fake_ensure)

    result = await pf.reprobe_source_permission(db, "documents")
    assert result is not None and result["status"] == "authorized"
    # Probed against the freshly-picked root, not None.
    assert seen == {"name": "documents", "root": "/tmp/docs"}

    import json

    async with db.execute(
        "SELECT value FROM settings WHERE key = 'source_documents_permission'"
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row[0])["status"] == "authorized"


async def test_reprobe_is_noop_for_disabled_source(wired_tools_db, monkeypatch):
    """A disabled source must never trigger a prompt — reprobe is a no-op."""
    from estormi_server.server import permission_preflight as pf
    from estormi_server.server import permissions as perms
    from estormi_server.storage import tools

    db = tools._db
    await db.execute("INSERT INTO settings(key,value) VALUES('source_documents_enabled','false')")
    await db.execute("INSERT INTO settings(key,value) VALUES('documents_root','/tmp/docs')")
    await db.commit()

    called = False

    def _fake_ensure(name, root=None):
        nonlocal called
        called = True
        return {"status": "authorized"}

    monkeypatch.setattr(perms, "ensure_source_permission", _fake_ensure)

    assert await pf.reprobe_source_permission(db, "documents") is None
    assert called is False


async def test_reprobe_is_noop_for_permissionless_and_unknown_sources(wired_tools_db):
    """Sources needing no macOS permission (gcal) and unknown names short-circuit
    before any probe — `<key>_root` keys that aren't real source roots are safe."""
    from estormi_server.server import permission_preflight as pf
    from estormi_server.storage import tools

    db = tools._db
    assert await pf.reprobe_source_permission(db, "gcal") is None
    assert await pf.reprobe_source_permission(db, "does-not-exist") is None


async def test_preflight_extra_paths_feed_the_volume_probe(wired_tools_db, monkeypatch):
    from estormi_server.server import permission_preflight as pf
    from estormi_server.server import permissions as perms
    from estormi_server.storage import tools

    db = tools._db
    await db.execute(
        "INSERT INTO settings(key,value) VALUES('preflight_extra_paths','/Volumes/SSD, /Volumes/Other')"
    )
    await db.commit()

    seen: dict[str, tuple] = {}

    def _probe(extra=()):
        seen["extra"] = tuple(extra)
        return []

    monkeypatch.setattr(perms, "ensure_source_permission", lambda name, root=None: None)
    monkeypatch.setattr(perms, "probe_working_set_volumes", _probe)

    await pf.run_preflight()
    assert "/Volumes/SSD" in seen["extra"]
    assert "/Volumes/Other" in seen["extra"]
