"""Architectural contract: the one-way dependency rule is enforced, not just documented.

CLAUDE.md ("Layering") states the dependency direction:

    apps/*  ->  estormi_server/  ->  connectors  ->  memory_core

This test runs Import Linter (``[tool.importlinter]`` in ``pyproject.toml``)
in-process so the layering contracts are checked on every CI run. ``conftest``
adds the repo root and ``packages/`` to ``sys.path``, so all six root packages
(``memory_core``, ``connectors``, ``estormi_server``, ``estormi_ingestion``,
``estormi_briefing``, ``estormi_distill``) resolve as packages — exactly what
grimp needs to build
the import graph, with no subprocess / PYTHONPATH plumbing here.

A failure means a lower layer reached upward (e.g. ``memory_core`` importing a
server module, or ``connectors`` importing an ingestion script). See
``[tool.importlinter]`` in ``pyproject.toml`` for the full set of contracts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_import_layers_are_kept():
    from importlinter.api import use_cases

    # ``lint_imports`` resolves ``config_filename`` relative to the cwd, so run
    # it from the repo root regardless of where pytest was invoked.
    prev_cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        result = use_cases.lint_imports(
            config_filename="pyproject.toml",
            cache_dir=None,  # don't litter the tree with a grimp cache
        )
    finally:
        os.chdir(prev_cwd)

    assert result == use_cases.SUCCESS, (
        "Import Linter found a layering violation — a lower layer reached "
        "upward. Run `lint-imports` from the repo root (with packages/ on "
        "PYTHONPATH) for the offending import chain."
    )
