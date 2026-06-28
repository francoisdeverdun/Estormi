"""FastAPI app exposing MCP tools as HTTP + SSE endpoints.

Implements the subset of the MCP HTTP/SSE transport that Claude Desktop and
claude.ai use:
  - POST /mcp   — JSON-RPC 2.0 requests (initialize, tools/list, tools/call)
  - GET  /sse   — Server-Sent Events stream for push notifications
  - POST /ingest_chunk + /search_memory — plain REST shims for pipeline scripts
  - GET  /health

The server binds on localhost (127.0.0.1) by default so it is only reachable
from the local machine. LAN access is opt-in via settings.

This module is intentionally thin: it wires together the FastAPI app,
middleware stack, lifespan, static mounts, and the per-domain routers
defined under ``api/`` and ``server/``. The behaviour-bearing code lives
in those submodules.
"""

from __future__ import annotations

import os

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from estormi_server import __version__

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

load_dotenv()

# Relocate the library if the user queued a move (the storage API writes a
# marker; it does not move anything). This MUST run before any import of
# ``memory_core.settings``: ``DATA_DIR``/``DB_PATH`` are constants frozen at
# that import and the DB opens from them in the lifespan, so the relocation
# pointer has to be flipped first. The resolver deliberately lives in
# ``memory_core.datadir`` so this depends on nothing that would freeze the path.
# No-op when there is no pending move (the common case).
from memory_core.datadir import bootstrap_relocate, resolve_data_dir  # noqa: E402

bootstrap_relocate(log=structlog.get_logger("estormi.datadir"))
# Pin the resolved library path into this process's environment so EVERY engine
# subprocess inherits it via ``os.environ.copy()``. This is load-bearing for the
# relocatable data dir: ``scripts/daily_ingestion.sh`` falls back to a hardcoded
# ``~/Library/Application Support/Estormi`` and *exports* it to its Python
# stages, so without an explicit pin a relocated library would split-brain
# (server reads the pointer, scheduled ingestion writes the old default).
# ``setdefault`` keeps an explicit env override (dev/tests) authoritative.
os.environ.setdefault("ESTORMI_DATA_DIR", resolve_data_dir())

# Routers — load_dotenv() must run before any sub-module is imported (the
# job/scheduler modules read env vars at import time), so the noqa-E402
# marker is required on every router import line below.
from estormi_server.api import (  # noqa: E402
    calendar_oauth,
    distill,
    events,
    ingest,
    knowledge,
    mcp_rpc,
    model,
    search,
    system,
    tts,
    whoop_oauth,
)
from estormi_server.api import dashboard as dashboard_api  # noqa: E402
from estormi_server.api import jobs as jobs_api  # noqa: E402
from estormi_server.api import permissions as permissions_api  # noqa: E402
from estormi_server.api import pipeline as pipeline_api  # noqa: E402
from estormi_server.api import settings as settings_api  # noqa: E402
from estormi_server.api import settings_ui as settings_ui_api  # noqa: E402
from estormi_server.api import storage as storage_api  # noqa: E402
from estormi_server.server.lifespan import lifespan  # noqa: E402
from estormi_server.server.limiter import limiter  # noqa: E402
from estormi_server.server.security import security_boundary  # noqa: E402
from estormi_server.server.static import register_static_mounts  # noqa: E402

app = FastAPI(
    title="Estormi MCP",
    description="Private local memory — notes, mails, messages, and docs",
    # Cosmetic only (OpenAPI is disabled below, so this is never rendered).
    # Single source of truth in estormi_server.__version__ — keep that in sync
    # with pyproject.toml / apps/estormi-macos/Cargo.toml / tauri.conf.json.
    version=__version__,
    lifespan=lifespan,
    # No live API docs surface: the interactive Swagger/ReDoc renders and the
    # OpenAPI schema are disabled. Reference docs live as static Markdown under
    # docs/ in the repo. (The security boundary already gates these paths, so
    # this is belt-and-braces — there is simply nothing to render.)
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.limiter = limiter
# slowapi's handler signature is looser than Starlette's ExceptionHandler type.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # pyright: ignore[reportArgumentType]

app.middleware("http")(security_boundary)

# Per-domain routers — see the docstrings on each module for ownership.
app.include_router(mcp_rpc.router)
app.include_router(ingest.router)
app.include_router(search.router)
app.include_router(system.router)
app.include_router(settings_api.router)
app.include_router(settings_ui_api.router)
app.include_router(dashboard_api.router)
app.include_router(pipeline_api.router)
app.include_router(permissions_api.router)
app.include_router(model.router)
app.include_router(tts.router)
app.include_router(knowledge.router)
app.include_router(calendar_oauth.router)
app.include_router(whoop_oauth.router)
app.include_router(events.router)
app.include_router(jobs_api.router)
app.include_router(distill.router)
app.include_router(storage_api.router)

# Favicon + /brand + /source-icons + /fonts + the SPA at /app/.
register_static_mounts(app)
