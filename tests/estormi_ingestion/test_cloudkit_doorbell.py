"""Unit tests for estormi_ingestion.shared.delivery.cloudkit_doorbell.

The real ring needs a signed helper and an iCloud session, so these tests pin
the pure logic: the opt-in gate, helper discovery, the codesign team check,
the exit-code mapping, and the never-raise contract. The subprocess boundary
is mocked throughout — no helper binary ever runs here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from estormi_ingestion.shared.delivery import cloudkit_doorbell

pytestmark = pytest.mark.unit


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixed config home — where the helper + config now canonically live.

    Pinned to a tmp dir so config_home() never leaks to the developer's real
    ``~/Library/Application Support/Estormi`` (which may hold a live helper)."""
    c = tmp_path / "config"
    c.mkdir()
    monkeypatch.setenv("ESTORMI_CONFIG_HOME", str(c))
    return c


@pytest.fixture
def data_dir(config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(d))
    monkeypatch.delenv("ESTORMI_DOORBELL_HELPER", raising=False)
    monkeypatch.delenv("ESTORMI_DOORBELL_ENABLED", raising=False)
    monkeypatch.delenv("ESTORMI_DOORBELL_TEAM_ID", raising=False)
    # The team-id resolver also falls back to ESTORMI_APNS_TEAM_ID — clear it so
    # these tests stay hermetic on a machine that has APNs configured.
    monkeypatch.delenv("ESTORMI_APNS_TEAM_ID", raising=False)
    return d


def _install_helper(app_dir: Path) -> Path:
    """Write an installed-looking helper app (executable bit set, never run)."""
    binary = app_dir / "Contents" / "MacOS" / "EstormiCloud"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return app_dir


@pytest.fixture
def fake_helper(data_dir: Path) -> Path:
    """A legacy helper install under the (movable) data dir — found via fallback."""
    return _install_helper(data_dir / "bin" / "EstormiCloud.app")


@pytest.fixture
def fake_helper_config_home(config_dir: Path, data_dir: Path) -> Path:
    """A helper installed at the canonical, relocation-immune config home."""
    return _install_helper(config_dir / "bin" / "EstormiCloud.app")


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# gating: installation + opt-in
# ---------------------------------------------------------------------------


def test_not_configured_without_helper(data_dir: Path) -> None:
    assert cloudkit_doorbell.is_configured() is False


def test_configured_with_helper(fake_helper: Path) -> None:
    assert cloudkit_doorbell.is_configured() is True


def test_disabled_by_default(data_dir: Path) -> None:
    assert cloudkit_doorbell.is_enabled() is False


def test_enabled_via_config_file(data_dir: Path) -> None:
    (data_dir / "doorbell_config.json").write_text(json.dumps({"enabled": True}))
    assert cloudkit_doorbell.is_enabled() is True


def test_env_var_overrides_config_file(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (data_dir / "doorbell_config.json").write_text(json.dumps({"enabled": True}))
    monkeypatch.setenv("ESTORMI_DOORBELL_ENABLED", "0")
    assert cloudkit_doorbell.is_enabled() is False


def test_unparseable_config_means_disabled(data_dir: Path) -> None:
    (data_dir / "doorbell_config.json").write_text("{not json")
    assert cloudkit_doorbell.is_enabled() is False


# ---------------------------------------------------------------------------
# codesign team pinning
# ---------------------------------------------------------------------------


def test_verify_team_accepts_pinned_team(
    fake_helper: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Team id pinned via doorbell_config.json (the file-config path); a codesign
    # TeamIdentifier matching the configured team passes.
    (data_dir / "doorbell_config.json").write_text(json.dumps({"team_id": "TEAMTEST00"}))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "TeamIdentifier=TEAMTEST00\n")
    )
    assert cloudkit_doorbell._verify_team(fake_helper) is True


def test_verify_team_refuses_when_no_team_configured(
    fake_helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No team id configured (no env var, no config file). The doorbell refuses
    # to trust *any* signature rather than falling back to a hardcoded default —
    # the secure posture after the owner's real Team ID was removed from source.
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "TeamIdentifier=ANYTEAM000\n")
    )
    assert cloudkit_doorbell._verify_team(fake_helper) is False


def test_verify_team_rejects_other_team(fake_helper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "TeamIdentifier=EVIL000000\n")
    )
    assert cloudkit_doorbell._verify_team(fake_helper) is False


def test_verify_team_rejects_adhoc_signature(
    fake_helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "TeamIdentifier=not set\n")
    )
    assert cloudkit_doorbell._verify_team(fake_helper) is False


def test_verify_team_rejects_when_codesign_says_nothing(
    fake_helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(1, "code object not signed"))
    assert cloudkit_doorbell._verify_team(fake_helper) is False


def test_verify_team_env_override(fake_helper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_DOORBELL_TEAM_ID", "SELFBUILD99")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "TeamIdentifier=SELFBUILD99\n")
    )
    assert cloudkit_doorbell._verify_team(fake_helper) is True


def test_verify_team_refuses_tampered_seal_even_if_team_matches(
    fake_helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seal is validated BEFORE the team is parsed: `codesign --verify`
    re-hashes the code, so a tampered-but-signed helper is refused even though
    `-dv` would still report the pinned TeamIdentifier."""
    monkeypatch.setenv("ESTORMI_DOORBELL_TEAM_ID", "TEAMTEST00")

    def _run(args, *a, **k):
        if "--verify" in args:  # the seal check fails (bytes no longer match)
            return _completed(2, "a sealed resource is missing or invalid")
        return _completed(0, "TeamIdentifier=TEAMTEST00\n")

    monkeypatch.setattr(subprocess, "run", _run)
    assert cloudkit_doorbell._verify_team(fake_helper) is False


# ---------------------------------------------------------------------------
# send_doorbell: gates fire before any subprocess
# ---------------------------------------------------------------------------


def test_send_noop_when_disabled(fake_helper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # pragma: no cover — must never run
        raise AssertionError("subprocess must not be spawned when disabled")

    monkeypatch.setattr(subprocess, "run", boom)
    assert cloudkit_doorbell.send_doorbell("t", "b", "2026-06-12") is False


def test_send_noop_when_helper_missing(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_DOORBELL_ENABLED", "1")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("subprocess must not be spawned without a helper")

    monkeypatch.setattr(subprocess, "run", boom)
    assert cloudkit_doorbell.send_doorbell("t", "b", "2026-06-12") is False


def test_send_refuses_unverified_helper(fake_helper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_DOORBELL_ENABLED", "1")
    monkeypatch.setattr(cloudkit_doorbell, "_verify_team", lambda app: False)

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("helper must not be spawned when the signature check fails")

    monkeypatch.setattr(subprocess, "run", boom)
    assert cloudkit_doorbell.send_doorbell("t", "b", "2026-06-12") is False


# ---------------------------------------------------------------------------
# send_doorbell: exit-code contract with main.swift
# ---------------------------------------------------------------------------


@pytest.fixture
def ringable(fake_helper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTORMI_DOORBELL_ENABLED", "1")
    monkeypatch.setattr(cloudkit_doorbell, "_verify_team", lambda app: True)


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, True), (1, False), (2, False), (3, False), (64, False)],
)
def test_send_maps_exit_codes(
    ringable: None, monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed(returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cloudkit_doorbell.send_doorbell("New briefing", "ready", "2026-06-12") is expected
    assert len(calls) == 1
    assert calls[0][1:] == ["--title", "New briefing", "--body", "ready", "--date", "2026-06-12"]


def test_send_false_on_timeout(ringable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cloudkit_doorbell.send_doorbell("t", "b", "2026-06-12") is False


def test_send_never_raises(ringable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cloudkit_doorbell.send_doorbell("t", "b", "2026-06-12") is False


# ---------------------------------------------------------------------------
# helper location: config home (relocation-immune) wins; legacy is a fallback
# ---------------------------------------------------------------------------


def test_helper_resolves_at_config_home(fake_helper_config_home: Path) -> None:
    assert cloudkit_doorbell.is_configured() is True
    assert cloudkit_doorbell._helper_app() == fake_helper_config_home


def test_legacy_helper_still_found(fake_helper: Path) -> None:
    # No config-home install → the resolver falls back to the legacy data-dir
    # location, so an existing pre-migration setup keeps working.
    assert cloudkit_doorbell.is_configured() is True
    assert cloudkit_doorbell._helper_app() == fake_helper


def test_config_home_helper_wins_over_legacy(
    fake_helper: Path, fake_helper_config_home: Path
) -> None:
    assert cloudkit_doorbell._helper_app() == fake_helper_config_home


def test_default_helper_path_is_config_home(data_dir: Path, config_dir: Path) -> None:
    # Nothing installed anywhere → messaging points at the canonical location.
    assert cloudkit_doorbell._helper_app() == config_dir / "bin" / "EstormiCloud.app"


def test_enabled_via_config_home_file(config_dir: Path, data_dir: Path) -> None:
    (config_dir / "doorbell_config.json").write_text(json.dumps({"enabled": True}))
    assert cloudkit_doorbell.is_enabled() is True


def test_config_home_file_wins_over_legacy(config_dir: Path, data_dir: Path) -> None:
    (config_dir / "doorbell_config.json").write_text(json.dumps({"enabled": True}))
    (data_dir / "doorbell_config.json").write_text(json.dumps({"enabled": False}))
    assert cloudkit_doorbell.is_enabled() is True


# ---------------------------------------------------------------------------
# migrate_helper_to_config_home: promote a legacy install to the fixed home
# ---------------------------------------------------------------------------


def _ditto_and_codesign(*, verify_rc: int = 0):
    """A subprocess.run stand-in: ditto materialises the dest .app, codesign
    --verify returns ``verify_rc``."""

    def fake_run(cmd, **kwargs):
        if cmd[0].endswith("ditto"):
            _install_helper(Path(cmd[2]))
            return _completed(0)
        if cmd[0].endswith("codesign"):
            return _completed(verify_rc)
        raise AssertionError(f"unexpected subprocess: {cmd}")  # pragma: no cover

    return fake_run


def test_migrate_promotes_legacy_helper(
    fake_helper: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", _ditto_and_codesign())
    assert cloudkit_doorbell.migrate_helper_to_config_home() is True
    assert (config_dir / "bin" / "EstormiCloud.app").exists()
    assert not fake_helper.exists()  # legacy dropped after a verified copy


def test_migrate_carries_config_file(
    fake_helper: Path, data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_dir / "doorbell_config.json").write_text(json.dumps({"enabled": True}))
    monkeypatch.setattr(subprocess, "run", _ditto_and_codesign())
    assert cloudkit_doorbell.migrate_helper_to_config_home() is True
    assert (config_dir / "doorbell_config.json").is_file()
    assert not (data_dir / "doorbell_config.json").is_file()


def test_migrate_noop_when_config_home_present(
    fake_helper: Path, fake_helper_config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **k):  # pragma: no cover — must never copy over an install
        raise AssertionError("migration must not run when the config home is populated")

    monkeypatch.setattr(subprocess, "run", boom)
    assert cloudkit_doorbell.migrate_helper_to_config_home() is False


def test_migrate_noop_when_nothing_to_migrate(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("migration must not run with no legacy helper")

    monkeypatch.setattr(subprocess, "run", boom)
    assert cloudkit_doorbell.migrate_helper_to_config_home() is False


def test_migrate_keeps_legacy_on_verify_failure(
    fake_helper: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", _ditto_and_codesign(verify_rc=1))
    assert cloudkit_doorbell.migrate_helper_to_config_home() is False
    assert fake_helper.exists()  # legacy untouched
    assert not (config_dir / "bin" / "EstormiCloud.app").exists()  # broken copy cleaned up


def test_migrate_never_raises(
    fake_helper: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd, **kwargs):
        raise OSError("ditto blew up")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cloudkit_doorbell.migrate_helper_to_config_home() is False
