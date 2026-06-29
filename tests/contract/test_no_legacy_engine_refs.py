"""The retired engines stay retired.

The big pre-public refactor collapsed four engines to two (Ingestion +
Briefing) and deleted the Extraction and Correlation engines, with correlation
now emergent from ``fetch_around`` retrieval (see
``docs/architecture/rationale.md``). Their tables were dropped and must not
creep back into live code as if those features still exist — they survive only
in the ``DROP TABLE`` migration that removes them and the docs that record the
decision.

Each forbidden token carries an explicit allow-list of the files that may
legitimately mention it (the migration and the docs that record the removal).
A hit anywhere else fails the test — that is the regression lock.

The legacy brand names (*Shrine*, *Lesceline*, *The Memory Seal*) are banned
outright: the data-dir migration shim that once carried them was removed, so
nothing in the repo may mention them anymore.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]

# (token, reason, allow-listed repo-relative paths that may contain it).
# Paths are matched as prefixes so a whole doc tree can be allow-listed.
FORBIDDEN: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "entity_extraction_runs",
        "table from the deleted Extraction engine — only DROPped / documented",
        ("packages/estormi_server/sql/schema_migrations.py", "docs/migrations.md"),
    ),
    (
        "correlation_runs",
        "table from the deleted Correlation engine — only DROPped / documented",
        ("packages/estormi_server/sql/schema_migrations.py", "docs/migrations.md"),
    ),
    (
        "resolved_entities",
        "view from the deleted Extraction engine — only DROPped / documented",
        ("packages/estormi_server/sql/schema_migrations.py", "docs/migrations.md"),
    ),
    # Legacy brand names — fully retired, no surviving shim. Lowercase variants
    # are listed separately because matching is plain case-sensitive `in`.
    ("Shrine", "legacy internal product name — fully retired", ()),
    ("shrine", "legacy internal product name — fully retired", ()),
    ("Lesceline", "former iOS companion name — fully retired", ()),
    ("lesceline", "former iOS companion name — fully retired", ()),
    ("Memory Seal", "legacy tagline — fully retired", ()),
)


def _tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = []
    for line in out.splitlines():
        # This test file names every token, so skip it.
        if line == "tests/contract/test_no_legacy_engine_refs.py":
            continue
        # Binary / lockfile noise we never want to scan.
        if line.endswith((".png", ".svg", ".woff2", ".ttf", ".icns", ".lock", ".jpg")) or line in (
            "pnpm-lock.yaml",
            ".github/.secrets.baseline",
        ):
            continue
        files.append(line)
    return files


@pytest.mark.parametrize(
    "token, reason, allowed", FORBIDDEN, ids=lambda v: v if isinstance(v, str) else ""
)
def test_forbidden_token_absent(token: str, reason: str, allowed: tuple[str, ...]) -> None:
    offenders: list[str] = []
    for rel in _tracked_text_files():
        if any(rel == a or rel.startswith(a) for a in allowed):
            continue
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if token in text:
            offenders.append(rel)
    assert not offenders, (
        f"'{token}' is forbidden ({reason}); found in: {sorted(offenders)}. "
        f"If a hit is legitimate history, add its path to this token's allow-list."
    )
