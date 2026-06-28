"""CLI dispatch contract for ``python -m connectors`` (connectors/__main__.py).

``scripts/daily_ingestion.sh`` derives the nightly stage list from
``connectors stages`` and runs each stage with ``connectors run <stage>``,
branching on the exit code: ``0`` ok, ``1`` failed/unknown, ``SKIP_EXIT_CODE``
(75) permission-skip. These tests pin that exit-code contract and the dispatch,
with the registry + permission gate stubbed so no real connector subprocess runs.
"""

from __future__ import annotations

import pytest

from connectors import __main__ as cli
from connectors.base import Connector, ConnectorResult, ConnectorSpec
from connectors.permission_gate import SKIP_EXIT_CODE

pytestmark = pytest.mark.unit


def test_stages_prints_default_list(capsys):
    assert cli.main(["stages"]) == 0
    out = capsys.readouterr().out.split()
    # The nightly defaults are printed; the non-default gcal/whoop are not.
    assert "notes" in out
    assert "imessage" in out
    assert "gcal" not in out
    assert "whoop" not in out


def test_stages_all_includes_non_default(capsys):
    assert cli.main(["stages", "--all"]) == 0
    out = capsys.readouterr().out.split()
    assert "gcal" in out
    assert "whoop" in out


def test_run_unknown_stage_returns_1(capsys):
    assert cli.main(["run", "does-not-exist"]) == 1
    assert "unknown stage" in capsys.readouterr().err


def _install_stub(monkeypatch, *, errors=None, macos_permissions=(), permissions_optional=False):
    """Register a one-off stub connector as the registry's ``stub`` stage."""

    class _Stub(Connector):
        spec = ConnectorSpec(
            name="stub",
            title="Stub",
            description="fixture connector for CLI tests",
            macos_permissions=list(macos_permissions),
            permissions_optional=permissions_optional,
        )

        def ingest(self, **kwargs):
            return ConnectorResult(source="stub", errors=list(errors or []))

    monkeypatch.setattr(cli.registry, "get", lambda name: _Stub if name == "stub" else None)
    return _Stub


def test_run_ok_returns_0(monkeypatch, capsys):
    _install_stub(monkeypatch)
    assert cli.main(["run", "stub"]) == 0
    assert "ok" in capsys.readouterr().out


def test_run_failed_returns_1(monkeypatch, capsys):
    _install_stub(monkeypatch, errors=["boom"])
    assert cli.main(["run", "stub"]) == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert "FAILED" in captured.out


def test_run_permission_blocked_returns_skip_code(monkeypatch, capsys):
    _install_stub(monkeypatch, macos_permissions=["contacts"], permissions_optional=False)
    monkeypatch.setattr(cli.permission_gate, "persisted_permission_status", lambda s: "denied")
    monkeypatch.setattr(cli.permission_gate, "is_blocked_status", lambda s: True)
    assert cli.main(["run", "stub"]) == SKIP_EXIT_CODE
    assert "SKIPPED" in capsys.readouterr().out


def test_run_optional_permission_does_not_skip(monkeypatch, capsys):
    # An optional permission (e.g. WhatsApp's Contacts) must NOT skip the stage
    # even when blocked — the connector still runs.
    _install_stub(monkeypatch, macos_permissions=["contacts"], permissions_optional=True)
    blocked_checked = False

    def _never(_status):
        nonlocal blocked_checked
        blocked_checked = True
        return True

    monkeypatch.setattr(cli.permission_gate, "is_blocked_status", _never)
    assert cli.main(["run", "stub"]) == 0
    assert blocked_checked is False  # the gate is never consulted for optional perms
