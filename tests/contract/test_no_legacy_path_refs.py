"""Legacy package paths stay buried.

The two Python roots were renamed ``mcp-server`` → ``estormi_server`` and
``ingestion`` → ``estormi_ingestion``. The import-time code moved with the
rename, but path *references* in prose, comments, docstrings, CI commands and
config do not fail the build when they go stale — they just quietly mislead
the next reader (and, in a few cases, broke runtime: a hard-coded
``mcp-server/build_version.txt`` read and a ``ruff check ... ingestion``
release step that no longer resolved).

This contract scans the git-tracked working tree for the old *path* tokens and
fails if any reappear. It deliberately only flags the slash-anchored path forms
(``mcp-server/``, ``ingestion/``) — the bare words ``mcp-server`` and
``ingestion`` survive legitimately as the names of the task-scoped skills under
``.claude/skills/`` and as plain English ("the ingestion pipeline"), so
matching those would be all false positives. Scanning git-tracked files only
keeps generated build artefacts (coverage HTML, Xcode build outputs) out of
scope — we police what is committed, not what is built.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]

# Text we author and want policed. Lockfiles and binaries are skipped by suffix.
SCANNED_SUFFIXES = {
    ".py", ".md", ".ts", ".tsx", ".js", ".swift", ".rs", ".sh", ".command",
    ".yml", ".yaml", ".toml", ".ini", ".css", ".j2", ".txt", ".plist",
    ".entitlements", ".html", ".applescript",
}  # fmt: skip
SCANNED_NAMES = {"Makefile", ".env.example", ".gitattributes"}

# This file necessarily names the legacy tokens; exclude it from its own scan.
SELF = Path(__file__).resolve()

# ``mcp-server/`` and ``ingestion/`` as package paths, with two carve-outs:
#   * ``estormi_ingestion/`` — the renamed root (the ``_`` before "ingestion"
#     defeats the ``\b`` anyway; the lookbehind documents intent).
#   * ``.claude/skills/mcp-server/`` and ``.claude/skills/ingestion/`` — the
#     task-scoped *skills* are still named ``mcp-server`` / ``ingestion``;
#     their on-disk directories are not the renamed code roots.
LEGACY = re.compile(r"(?<!skills/)mcp-server/|(?<!estormi_)(?<![\w-])(?<!skills/)ingestion/")


def _tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for rel in out.split("\0"):
        if not rel:
            continue
        path = REPO_ROOT / rel
        if path.resolve() == SELF or not path.is_file():
            continue
        if path.suffix not in SCANNED_SUFFIXES and path.name not in SCANNED_NAMES:
            continue
        yield path


def test_no_legacy_package_path_references():
    offenders: list[str] = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if LEGACY.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Stale legacy package paths (use estormi_server/ / estormi_ingestion/):\n"
        + "\n".join(offenders)
    )
