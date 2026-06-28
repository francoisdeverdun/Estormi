"""The ``briefings/<date>.json`` vault payload is the iOS companion's contract.

The Mac writes ``briefings/<date>.json`` into the iCloud Drive vault; the
read-only iOS app renders it. The payload is assembled as a plain ``dict`` in
``estormi_briefing.run_briefing`` (there is no schema model), so a renamed or
dropped top-level key ships silently and the companion shows a blank field.

This pins the top-level key set — plus the editable ``fields`` sub-keys — to a
committed golden, the same way ``test_mcp_tool_surface.py`` pins the MCP
catalogue and ``test_openapi_spec_current.py`` pins the HTTP surface. Values are
not snapshotted (they're per-day content); only the schema is.

Regenerate deliberately after an intended change:

    ESTORMI_UPDATE_BRIEFING_GOLDEN=1 pytest tests/contract/test_briefing_payload_schema.py
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from estormi_briefing.compose.build_daily_note import briefing_fields

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "briefing_payload_schema.json"
RUN_BRIEFING = REPO_ROOT / "packages" / "estormi_briefing" / "run_briefing.py"


def _payload_top_level_keys() -> list[str]:
    """Top-level string keys of the ``briefing = {...}`` literal that becomes
    the vault JSON. Found via AST so this never executes a full composition."""
    tree = ast.parse(RUN_BRIEFING.read_text(encoding="utf-8"))
    dict_literals: list[list[str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "briefing" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            dict_literals.append([k.value for k in node.value.keys if isinstance(k, ast.Constant)])
    assert dict_literals, "could not find the `briefing = {...}` payload literal in run_briefing.py"
    # The canonical payload is the richest literal (defensive against any other
    # `briefing = {...}` assignment).
    return sorted(max(dict_literals, key=len))


def _schema() -> dict:
    return {
        "top_level": _payload_top_level_keys(),
        "fields": sorted(briefing_fields("").keys()),
        # audioPath is attached conditionally (only when narration succeeds) by
        # io/delivery.py — documented as optional, not part of top_level.
        "optional_top_level": ["audioPath"],
    }


def test_briefing_payload_schema_matches_golden():
    current = _schema()
    if os.environ.get("ESTORMI_UPDATE_BRIEFING_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip("regenerated briefing-payload golden")
    assert GOLDEN.exists(), (
        "briefing-payload golden missing — regenerate with "
        "ESTORMI_UPDATE_BRIEFING_GOLDEN=1 pytest tests/contract/test_briefing_payload_schema.py"
    )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current == golden, (
        "briefings/<date>.json schema changed vs the committed golden "
        "(tests/contract/briefing_payload_schema.json). If intended, regenerate: "
        "ESTORMI_UPDATE_BRIEFING_GOLDEN=1 pytest "
        "tests/contract/test_briefing_payload_schema.py"
    )
