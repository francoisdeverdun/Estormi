"""Tests for the connector permission gate.

The gate keeps a pipeline run *request-free*: it reads the status the preflight
persisted and skips a stage rather than letting a connector trigger a macOS
prompt mid-run. See ``connectors/permission_gate.py`` and the
``_cmd_run`` chokepoint in ``connectors/__main__.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pytest

from connectors import __main__ as cli
from connectors import permission_gate as gate

pytestmark = pytest.mark.unit


def _seed_db(tmp_path, source: str, status: str | None):
    db = tmp_path / "estormi.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    if status is not None:
        conn.execute(
            "INSERT INTO settings VALUES (?, ?)",
            (f"source_{source}_permission", json.dumps({"status": status})),
        )
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize(
    "status,blocked",
    [
        (None, False),  # never probed → run; the preflight is the safety net
        ("authorized", False),
        ("denied", True),
        ("manual", True),
        ("undetermined", True),
        ("unavailable", True),
    ],
)
def test_is_blocked_status(status, blocked):
    assert gate.is_blocked_status(status) is blocked


def test_persisted_status_reads_db(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "notes", "authorized")
    monkeypatch.setattr("memory_core.dag_state.db_path", lambda: str(db))
    assert gate.persisted_permission_status("notes") == "authorized"


def test_persisted_status_missing_key_is_none(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "notes", None)
    monkeypatch.setattr("memory_core.dag_state.db_path", lambda: str(db))
    assert gate.persisted_permission_status("notes") is None


def test_persisted_status_unreadable_db_is_none(monkeypatch):
    # The gate must fail *open* — a missing DB never wedges ingestion.
    monkeypatch.setattr("memory_core.dag_state.db_path", lambda: "/nope/estormi.db")
    assert gate.persisted_permission_status("notes") is None


# ── _cmd_run gating ───────────────────────────────────────────────────────────


class _Result:
    ok = True
    duration_ms = 0
    errors: list[str] = []


def _fake_connector(perms: tuple[str, ...], *, optional: bool = False):
    class _Spec:
        macos_permissions = perms
        permissions_optional = optional

    class _Conn:
        spec = _Spec()

        def ingest(self):  # noqa: D401 — stub
            return _Result()

    return _Conn


def test_cmd_run_skips_when_blocked(monkeypatch, capsys):
    monkeypatch.setattr(cli.registry, "get", lambda name: _fake_connector(("Reminders",)))
    monkeypatch.setattr(gate, "persisted_permission_status", lambda name: "denied")
    rc = cli._cmd_run(argparse.Namespace(stage="reminders"))
    assert rc == gate.SKIP_EXIT_CODE
    assert "SKIPPED" in capsys.readouterr().out


def test_cmd_run_runs_when_authorized(monkeypatch):
    monkeypatch.setattr(cli.registry, "get", lambda name: _fake_connector(("Reminders",)))
    monkeypatch.setattr(gate, "persisted_permission_status", lambda name: "authorized")
    assert cli._cmd_run(argparse.Namespace(stage="reminders")) == 0


def test_cmd_run_runs_when_optional_permission_denied(monkeypatch):
    # A connector whose macOS permission is declared optional (e.g. WhatsApp's
    # Contacts) must NOT be skipped when that permission is denied — it degrades
    # gracefully instead. Regression for the gate skipping the whole stage.
    monkeypatch.setattr(
        cli.registry, "get", lambda name: _fake_connector(("Contacts",), optional=True)
    )
    monkeypatch.setattr(gate, "persisted_permission_status", lambda name: "denied")
    assert cli._cmd_run(argparse.Namespace(stage="whatsapp")) == 0


def test_cmd_run_never_gates_permissionless_source(monkeypatch):
    # A connector with no macOS permission is never gated — and the gate is
    # never even consulted (would raise if it were, proving the short-circuit).
    monkeypatch.setattr(cli.registry, "get", lambda name: _fake_connector(()))
    monkeypatch.setattr(
        gate,
        "persisted_permission_status",
        lambda name: pytest.fail("gate consulted for a permissionless source"),
    )
    assert cli._cmd_run(argparse.Namespace(stage="gcal")) == 0
