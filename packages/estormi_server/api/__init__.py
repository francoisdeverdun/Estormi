"""HTTP route modules for the Estormi MCP FastAPI app.

Each submodule exposes an ``APIRouter`` (typically ``router``) that
``main.py`` includes into the application. The split preserves the
historical URL layout exactly — the goal of this package is purely to
narrow the surface of each file so they can be reviewed in isolation.
"""
