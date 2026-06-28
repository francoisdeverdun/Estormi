"""Canonical path/env defaults for memory-core.

:func:`resolve_data_dir` is the single implementation of the
``ESTORMI_DATA_DIR`` contract — the server (``estormi_server/storage/tools.py``) and
the ingestion scripts (``estormi_ingestion/shared/paths.py``) both consume it
instead of re-deriving the path, so an override containing ``~`` resolves
identically everywhere. The resolver itself lives in :mod:`memory_core.datadir`
(env → relocation pointer → default) and is re-exported here so existing callers
keep importing it from ``memory_core.settings``.
"""

from __future__ import annotations

import os

from memory_core.datadir import resolve_data_dir

# ── Data directory ────────────────────────────────────────────────────────────


def ensure_private_dir(path: str) -> str:
    """Create ``path`` (and parents) and lock it to owner-only ``0o700``.

    The Estormi data dir is the umbrella over every secret and PII file the
    app writes (the SQLite chunk store, OAuth tokens, the contacts index, the
    model cache). On a multi-user macOS box the default umask leaves new
    directories world-readable, so tighten the mode after creation. The
    ``chmod`` is best-effort — some filesystems (network/exotic) reject it —
    and a failure must not crash startup, so swallow ``OSError``.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


DATA_DIR: str = resolve_data_dir()

# ── Database ──────────────────────────────────────────────────────────────────

DB_PATH: str = os.path.join(DATA_DIR, "estormi.db")

# ── Logging ───────────────────────────────────────────────────────────────────

AUDIT_LOG_PATH: str = os.getenv("AUDIT_LOG_PATH", os.path.join(DATA_DIR, "audit.log"))

# Roll the audit log over once it crosses this many bytes. Keeps one backup
# alongside the live file. Default 16 MiB — long-running Mac installs would
# otherwise grow the log without bound. Set ``AUDIT_LOG_MAX_BYTES=0`` to
# disable rotation (tests).
AUDIT_LOG_MAX_BYTES: int = int(os.getenv("AUDIT_LOG_MAX_BYTES", str(16 * 1024 * 1024)))

# ── Auth ──────────────────────────────────────────────────────────────────────

MCP_TOKEN_ENV_VARS: list[str] = [
    "ESTORMI_MCP_TOKEN",
    "MCP_BEARER_TOKEN",
    "MCP_TOKEN",
]

__all__ = [
    "resolve_data_dir",
    "ensure_private_dir",
    "DATA_DIR",
    "DB_PATH",
    "AUDIT_LOG_PATH",
    "AUDIT_LOG_MAX_BYTES",
    "MCP_TOKEN_ENV_VARS",
]
