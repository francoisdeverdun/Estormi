"""Estormi server package — FastAPI HTTP + MCP transport and the two engines."""

# Single source of truth for the running server's version. Surfaced by the
# FastAPI app metadata (main.py) and the MCP ``initialize`` serverInfo response
# (api/mcp_rpc.py). Keep in sync with pyproject.toml / apps/estormi-macos/Cargo.toml /
# tauri.conf.json.
__version__ = "0.0.2"
