"""Single source of truth for ingestion-side data-dir resolution.

The ``ESTORMI_DATA_DIR`` contract itself is implemented once, in
``memory_core.settings.resolve_data_dir`` (the bottom layer both the server
and the ingestion scripts depend on), so the two can never drift. This module
keeps the ingestion-facing ``Path`` API and layers the per-script SQLite
override on top.

Resolution order (see ``resolve_data_dir``):

1. ``$ESTORMI_DATA_DIR`` — wins outright when set. Used by the Tauri
   bundle to point ingestion at the app's per-bundle Application Support
   path on macOS.
2. ``~/Library/Application Support/Estormi`` — the default when running
   the unbundled dev tree on a developer's machine.

``estormi_db_path`` then layers ``$ESTORMI_DB`` on top so individual
scripts can be pointed at an alternative SQLite file (used by tests).
"""

from __future__ import annotations

import os
from pathlib import Path

from memory_core.settings import resolve_data_dir


def estormi_data_dir() -> Path:
    """Return the absolute path of the Estormi data directory.

    See module docstring for the env-var precedence.
    """
    return Path(resolve_data_dir())


def estormi_db_path() -> str:
    """Return the absolute SQLite path for ingestion scripts.

    Layered on top of ``estormi_data_dir`` so ``ESTORMI_DB`` keeps its
    role as a per-script database override (used by tests and the
    occasional one-shot script).
    """
    override = os.environ.get("ESTORMI_DB")
    if override:
        return os.path.expanduser(override)
    return str(estormi_data_dir() / "estormi.db")
