"""Architectural contract: ``memory_core`` stays at the bottom.

CLAUDE.md hard rule:

    Do not put FastAPI routes in ``memory_core`` — it is the pure
    storage/retrieval layer; HTTP belongs in ``estormi_server/``.

``memory_core`` is the pure domain/support layer at the bottom of the
dependency chain (``apps -> mcp-server -> memory_core``). This test
walks every ``.py`` file under ``memory_core`` and parses it with
``ast``, asserting that none of them import an HTTP framework (``fastapi`` /
``starlette``) *or* reach upward into a higher layer (``server`` /
``mcp_server``, ``connectors``, ``ingestion``). A failure means the one-way
layering invariant has slipped and HTTP or an upper layer leaked into the
storage package.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_CORE = REPO_ROOT / "packages" / "memory_core"

# HTTP frameworks plus the upper layers memory_core must never import: the
# dependency direction is strictly one-way
# (apps -> estormi_server -> connectors -> memory_core). The real first-party
# package names are listed here — the old pre-rename labels (``server`` /
# ``mcp_server`` / ``ingestion``) never matched a real import, leaving this
# check toothless against the upper layers it claims to guard.
FORBIDDEN_TOP_LEVEL = {
    "fastapi",
    "starlette",
    "connectors",
    "estormi_server",
    "estormi_ingestion",
    "estormi_briefing",
    "estormi_distill",
}

EXCLUDED_PARTS = {"build", "__pycache__"}


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for p in MEMORY_CORE.rglob("*.py"):
        # Skip vendored / build artefacts and *.egg-info trees.
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


def test_memory_core_has_no_upper_layer_or_http_imports():
    if not MEMORY_CORE.is_dir():
        assert not os.environ.get("CI"), "memory_core missing in CI"
        pytest.skip("memory_core not present in this checkout")

    files = _iter_source_files()
    assert files, f"no python sources found under {MEMORY_CORE}"

    violations: dict[str, list[str]] = {}
    for path in files:
        bad = _forbidden_imports_in(path)
        if bad:
            violations[str(path.relative_to(REPO_ROOT))] = bad

    assert not violations, (
        "memory_core must stay HTTP-free and must not import upper layers "
        "(see CLAUDE.md hard rules):\n"
        + "\n".join(f"  {p}: {imports}" for p, imports in violations.items())
    )
