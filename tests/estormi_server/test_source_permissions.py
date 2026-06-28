"""Tests for server.permissions — per-source macOS permission orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import estormi_ingestion.shared.host.macos_permissions as mp_mod
from estormi_server.server import permissions as perms

pytestmark = pytest.mark.unit


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def test_source_permission_key_mapping():
    assert perms.source_permission_key("reminders") == "Reminders"
    assert perms.source_permission_key("imessage") == "FullDiskAccess"
    assert perms.source_permission_key("notes") == "AppleEvents:Notes"
    assert perms.source_permission_key("mail") == "AppleEvents:Mail"
    assert perms.source_permission_key("documents") == "FilesAndFolders"


def test_documents_probes_configured_root(monkeypatch, tmp_path):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    (tmp_path / "a.txt").write_text("x")
    result = perms.ensure_source_permission("documents", str(tmp_path))
    assert result is not None
    assert result["key"] == "FilesAndFolders"
    assert result["status"] == "authorized"
    # A clean grant carries no Settings link — nothing for the user to do.
    assert result["settings_pane"] is None


def test_documents_denied_root_surfaces_settings_pane(monkeypatch, tmp_path):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")

    def _denied(_root):
        raise PermissionError

    monkeypatch.setattr(perms.os, "scandir", _denied)
    result = perms.ensure_source_permission("documents", str(tmp_path))
    assert result is not None
    assert result["status"] == "denied"
    assert result["settings_pane"] == perms._PANE_FILES


def test_documents_unconfigured_root_is_undetermined(monkeypatch):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    result = perms.ensure_source_permission("documents", None)
    assert result is not None
    assert result["status"] == "undetermined"
    # The probe never ran — there's no folder to fix yet, so we still point
    # the user at the pane in case they want to pre-grant.
    assert result["settings_pane"] == perms._PANE_FILES


def test_sources_without_macos_permission_return_none():
    # Briefing / Google Calendar need no macOS TCC permission.
    assert perms.source_permission_key("briefing") is None
    assert perms.ensure_source_permission("briefing") is None
    assert perms.ensure_source_permission("gcal") is None
    assert perms.ensure_source_permission("does-not-exist") is None


def test_reminders_authorized(monkeypatch):
    monkeypatch.setattr(mp_mod, "request_reminders_access", lambda: True)
    monkeypatch.setattr(mp_mod, "get_reminders_status", lambda: "authorized")
    result = perms.ensure_source_permission("reminders")
    assert result is not None
    assert result["key"] == "Reminders"
    assert result["status"] == "authorized"
    # A clean grant carries no Settings link — nothing for the user to do.
    assert result["settings_pane"] is None


def test_reminders_denied_surfaces_settings_pane(monkeypatch):
    monkeypatch.setattr(mp_mod, "request_reminders_access", lambda: False)
    monkeypatch.setattr(mp_mod, "get_reminders_status", lambda: "denied")
    result = perms.ensure_source_permission("reminders")
    assert result is not None
    assert result["status"] == "denied"
    assert result["settings_pane"] == perms._PANE_REMINDERS


def test_reminders_not_determined_maps_to_undetermined(monkeypatch):
    monkeypatch.setattr(mp_mod, "request_reminders_access", lambda: False)
    monkeypatch.setattr(mp_mod, "get_reminders_status", lambda: "not_determined")
    result = perms.ensure_source_permission("reminders")
    assert result is not None
    assert result["status"] == "undetermined"
    assert result["settings_pane"] == perms._PANE_REMINDERS


def test_automation_authorized(monkeypatch):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(perms.subprocess, "run", lambda *a, **k: _Proc(0))
    result = perms.ensure_source_permission("notes")
    assert result is not None
    assert result["key"] == "AppleEvents:Notes"
    assert result["status"] == "authorized"


def test_automation_denied(monkeypatch):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        perms.subprocess,
        "run",
        lambda *a, **k: _Proc(1, "execution error: Not authorized to send Apple events (-1743)"),
    )
    result = perms.ensure_source_permission("mail")
    assert result is not None
    assert result["status"] == "denied"
    assert result["settings_pane"] == perms._PANE_AUTOMATION


def test_full_disk_access_reads_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(perms, "_full_disk_access_status", lambda: "authorized")
    result = perms.ensure_source_permission("imessage")
    assert result is not None
    assert result["key"] == "FullDiskAccess"
    assert result["status"] == "authorized"


def test_full_disk_access_manual_when_flag_absent():
    from estormi_server.storage.tools import DATA_DIR  # noqa: PLC0415

    flag = Path(DATA_DIR) / "imessage-fda.flag"
    if flag.exists():
        flag.unlink()
    result = perms.ensure_source_permission("imessage")
    assert result is not None
    assert result["status"] == "manual"
    # macOS has no FDA prompt — the user must be sent to System Settings.
    assert result["settings_pane"] == perms._PANE_ALL_FILES


def test_full_disk_access_absent_flag_is_authorized():
    """An "absent" flag (no chat.db) means nothing is FDA-gated → no nag."""
    from estormi_server.storage.tools import DATA_DIR  # noqa: PLC0415

    flag = Path(DATA_DIR) / "imessage-fda.flag"
    flag.write_text("absent", encoding="utf-8")
    try:
        assert perms._full_disk_access_status() == "authorized"
        result = perms.ensure_source_permission("imessage")
        assert result is not None
        assert result["status"] == "authorized"
        # Nothing for the user to fix → no Settings link.
        assert result["settings_pane"] is None
    finally:
        flag.unlink()


@pytest.mark.parametrize(
    "host_status,expected",
    [
        ("authorized", "authorized"),  # host copied the snapshot → FDA granted
        ("manual", "manual"),  # host denied → still needs granting
        ("weird", "unavailable"),  # unexpected payload → unavailable, not a crash
    ],
)
def test_recheck_full_disk_access_calls_loopback(monkeypatch, host_status, expected):
    """The re-check delegates to the FDA-covered Tauri host (loopback snapshot),
    since the Python sidecar can never confirm a grant itself."""
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("ESTORMI_WA_TOKEN", "tok")
    captured: dict = {}

    import urllib.request as urlreq  # noqa: PLC0415

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"status": host_status}).encode()

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["token"] = req.headers.get("X-estormi-wa-token")
        return _Resp()

    monkeypatch.setattr(urlreq, "urlopen", fake_urlopen)
    assert perms.recheck_full_disk_access() == expected
    assert captured["url"] == "http://127.0.0.1:9877/api/imessage/snapshot"
    assert captured["token"] == "tok"


def test_recheck_full_disk_access_non_macos(monkeypatch):
    monkeypatch.setattr(perms.platform, "system", lambda: "Linux")
    assert perms.recheck_full_disk_access() == "unavailable"


def test_recheck_full_disk_access_no_token(monkeypatch):
    """Without the shared token there is no host to ask → unavailable."""
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("ESTORMI_WA_TOKEN", raising=False)
    assert perms.recheck_full_disk_access() == "unavailable"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("authorized", "authorized"),
        ("denied", "denied"),
        ("restricted", "denied"),
        ("not_determined", "undetermined"),
        ("unavailable", "unavailable"),
    ],
)
def test_normalize_tcc(raw, expected):
    assert perms._normalize_tcc(raw) == expected


def test_whatsapp_requests_contacts(monkeypatch):
    # Activating WhatsApp fires + verifies the native Contacts permission.
    monkeypatch.setattr(mp_mod, "request_contacts_access", lambda: True)
    monkeypatch.setattr(mp_mod, "get_contacts_status", lambda: "authorized")
    result = perms.ensure_source_permission("whatsapp")
    assert result is not None
    assert result["key"] == "Contacts"
    assert result["status"] == "authorized"
    assert result["settings_pane"] is None


def test_whatsapp_contacts_denied_surfaces_settings_pane(monkeypatch):
    monkeypatch.setattr(mp_mod, "request_contacts_access", lambda: False)
    monkeypatch.setattr(mp_mod, "get_contacts_status", lambda: "denied")
    result = perms.ensure_source_permission("whatsapp")
    assert result is not None
    assert result["status"] == "denied"
    assert result["settings_pane"] == perms._PANE_CONTACTS


# ── Removable / external volumes ──────────────────────────────────────────────


def test_is_removable_classifies_mounts():
    assert perms._is_removable(Path("/Volumes/SSD")) is True
    assert perms._is_removable(Path("/")) is False
    # /Volumes itself is not a removable volume — its parent is /, not /Volumes.
    assert perms._is_removable(Path("/Volumes")) is False


def test_documents_on_removable_root_uses_removable_pane(monkeypatch, tmp_path):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(perms, "_is_removable", lambda vol: True)

    def _denied(_root):
        raise PermissionError

    monkeypatch.setattr(perms.os, "scandir", _denied)
    result = perms.ensure_source_permission("documents", str(tmp_path))
    assert result is not None
    assert result["label"] == "Removable-volume access"
    assert result["status"] == "denied"
    assert result["settings_pane"] == perms._PANE_REMOVABLE


def test_probe_working_set_volumes_dedupes_by_volume(monkeypatch):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    # Every candidate path maps to the same removable volume → one entry.
    monkeypatch.setattr(perms, "_volume_of", lambda p: Path("/Volumes/SSD"))
    monkeypatch.setattr(perms, "_is_removable", lambda vol: True)
    monkeypatch.setattr(perms, "_probe_filesystem", lambda p: "authorized")
    out = perms.probe_working_set_volumes(("/Volumes/SSD/root1", "/Volumes/SSD/root2"))
    assert len(out) == 1
    assert out[0]["key"] == "RemovableVolume:/Volumes/SSD"
    assert out[0]["status"] == "authorized"


def test_probe_working_set_volumes_empty_for_internal(monkeypatch):
    monkeypatch.setattr(perms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(perms, "_is_removable", lambda vol: False)
    assert perms.probe_working_set_volumes(("/Users/me/docs",)) == []
