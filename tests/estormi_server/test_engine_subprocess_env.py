"""engine_subprocess_env — the env for ``python -m estormi_<engine>`` children.

Regression guard for the bundle bug where briefing/distill died with
``ModuleNotFoundError: No module named 'estormi_distill'``: the engines run with
cwd=ROOT but the first-party packages live under ROOT/packages since the move,
and the bundled Python only pip-installs memory_core — so ROOT/packages MUST be
on PYTHONPATH.
"""

from __future__ import annotations

import os

import pytest

from estormi_server.server import jobs

pytestmark = pytest.mark.unit


def test_puts_packages_on_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(jobs, "_resolve_self_url", lambda: "http://127.0.0.1:8000")
    env = jobs.engine_subprocess_env()
    assert env["PYTHONPATH"] == str(jobs.ROOT / "packages")
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["MCP_SERVER_URL"] == "http://127.0.0.1:8000"


def test_prepends_to_existing_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/already/here")
    monkeypatch.setattr(jobs, "_resolve_self_url", lambda: "http://x")
    env = jobs.engine_subprocess_env()
    assert env["PYTHONPATH"] == f"{jobs.ROOT / 'packages'}{os.pathsep}/already/here"


def test_extra_kwargs_merge(monkeypatch):
    monkeypatch.setattr(jobs, "_resolve_self_url", lambda: "http://x")
    env = jobs.engine_subprocess_env(ESTORMI_BRIEFING_REFRESH="health")
    assert env["ESTORMI_BRIEFING_REFRESH"] == "health"
