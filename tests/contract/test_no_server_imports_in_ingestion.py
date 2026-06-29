"""Architectural contract: the three engines (``estormi_ingestion``,
``estormi_briefing``, ``estormi_distill``) must not import the server.

The dependency direction is one-way — the server drives the engines, never the
other way round:

    estormi_server/  ->  estormi_ingestion/, estormi_briefing/, estormi_distill/  ->  packages/*

For a while the Briefing engine's delivery step broke this by reaching back up
(``delivery.py`` did ``from estormi_server import tts_local``), making the
packages a genuine import cycle that only survived because the offending
imports were lazy, function-local ``# noqa: PLC0415`` work-arounds — invisible
to the import-linter, which only roots at ``memory_core`` / ``connectors``.
``tts_local`` now lives in ``memory_core`` (next to its sibling
``llm_local``), so the briefing engine imports it downward from the shared
layer and the cycle is gone.

This test walks every ``.py`` file under the three engine packages and parses
it with ``ast``, asserting none of them import ``estormi_server``. A failure
means the cycle has crept back in — relocate the shared dependency into
``packages/`` (the bottom layer every engine may import) instead.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
# All three engine packages must depend downward only.
GUARDED_PACKAGES = (
    REPO_ROOT / "packages" / "estormi_ingestion",
    REPO_ROOT / "packages" / "estormi_briefing",
    REPO_ROOT / "packages" / "estormi_distill",
)

# The one forbidden upward edge: neither may import the server package.
FORBIDDEN_TOP_LEVEL = {"estormi_server"}

EXCLUDED_PARTS = {"build", "__pycache__"}


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for root in GUARDED_PACKAGES:
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            if any(part in EXCLUDED_PARTS for part in p.parts):
                continue
            if any(part.endswith(".egg-info") for part in p.parts):
                continue
            files.append(p)
    return files


def _forbidden_imports_in(path: Path) -> list[str]:
    """Return forbidden top-level package names imported by ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # pragma: no cover — surfaced via assert below
        pytest.fail(f"{path}: failed to parse — {exc}")
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in FORBIDDEN_TOP_LEVEL:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".", 1)[0]
            if top in FORBIDDEN_TOP_LEVEL:
                offenders.append(module)
    return offenders


def test_engines_never_import_the_server():
    if not any(root.is_dir() for root in GUARDED_PACKAGES):
        assert not os.environ.get("CI"), "engine packages missing in CI"
        pytest.skip("engine packages not present in this checkout")

    files = _iter_source_files()
    assert files, f"no python sources found under {GUARDED_PACKAGES}"

    violations: dict[str, list[str]] = {}
    for path in files:
        bad = _forbidden_imports_in(path)
        if bad:
            violations[str(path.relative_to(REPO_ROOT))] = bad

    assert not violations, (
        "the engine packages (ingestion / briefing / distill) must not import "
        "estormi_server — it would re-form the package import cycle (see "
        "[tool.importlinter] in pyproject.toml). Move the shared dependency into "
        "packages/ instead:\n" + "\n".join(f"  {p}: {imports}" for p, imports in violations.items())
    )
