"""The MCP tool surface is a cross-process contract — snapshot it.

The MCP tools advertised by ``estormi_server.api.mcp_rpc.TOOLS`` are consumed by
external MCP clients (Claude Desktop / Claude Code). Unlike the HTTP surface —
pinned byte-for-byte by ``test_openapi_spec_current.py`` — the MCP catalogue is
hand-defined and is NOT part of OpenAPI, so a rename, a dropped ``required``
field, or a changed ``enum`` can ship silently and break a client.

This locks the STRUCTURAL surface (tool names, required fields, and each
property's name + type + enum) to a committed golden, and separately asserts
that every advertised tool has a dispatcher branch and vice versa — the two
halves that can drift apart inside ``mcp_rpc.py``.

Prose ``description`` text is intentionally excluded: it changes often and is
not a machine contract. Regenerate the golden deliberately after an intended
change:

    ESTORMI_UPDATE_MCP_GOLDEN=1 pytest tests/contract/test_mcp_tool_surface.py
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from estormi_server.api.mcp_rpc import TOOLS

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "mcp_tool_surface.json"
MCP_RPC = REPO_ROOT / "packages" / "estormi_server" / "api" / "mcp_rpc.py"


def _tool_surface() -> dict:
    """The structural (description-free) shape of the MCP tool catalogue."""
    surface: dict = {}
    for tool in TOOLS:
        schema = tool.get("inputSchema", {})
        props: dict = {}
        for pname, pdef in schema.get("properties", {}).items():
            entry = {"type": pdef.get("type")}
            if "enum" in pdef:
                entry["enum"] = pdef["enum"]
            props[pname] = entry
        surface[tool["name"]] = {
            "required": sorted(schema.get("required", [])),
            "properties": {k: props[k] for k in sorted(props)},
        }
    return dict(sorted(surface.items()))


def _dispatcher_tool_names() -> set[str]:
    """Tool names compared as ``if name == "..."`` inside ``_dispatch_tool``."""
    tree = ast.parse(MCP_RPC.read_text(encoding="utf-8"))
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_dispatch_tool"
    )
    names: set[str] = set()
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "name"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        ):
            names.add(node.comparators[0].value)
    return names


def test_mcp_tool_surface_matches_golden():
    current = _tool_surface()
    if os.environ.get("ESTORMI_UPDATE_MCP_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip("regenerated MCP tool-surface golden")
    assert GOLDEN.exists(), (
        "MCP tool-surface golden missing — regenerate with "
        "ESTORMI_UPDATE_MCP_GOLDEN=1 pytest tests/contract/test_mcp_tool_surface.py"
    )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current == golden, (
        "MCP tool surface changed vs the committed golden "
        "(tests/contract/mcp_tool_surface.json). If the change is intended, "
        "regenerate: ESTORMI_UPDATE_MCP_GOLDEN=1 pytest "
        "tests/contract/test_mcp_tool_surface.py"
    )


def test_every_tool_has_a_dispatcher_branch_and_vice_versa():
    advertised = {t["name"] for t in TOOLS}
    dispatched = _dispatcher_tool_names()
    assert advertised == dispatched, (
        "MCP TOOLS catalogue and _dispatch_tool are out of sync:\n"
        f"  advertised but not dispatched: {sorted(advertised - dispatched)}\n"
        f"  dispatched but not advertised: {sorted(dispatched - advertised)}"
    )
