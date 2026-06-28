"""Distillation workspace layout + status file.

Everything lives under ``<data dir>/distill/`` — personal data, never the
repo. The status file is the single contract with the API/UI: the engine
writes it at every phase boundary, ``GET /api/distill/status`` serves it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from memory_core.settings import resolve_data_dir

# Free-disk floor before the train/fuse phases may start: the dequantized
# F16 intermediate alone is ~28 GB for a 14B. The training corpus is the user's
# own briefing archive (mirrored into ``refs/`` by ``references.harvest_archive``),
# so the workspace tracks the vault — there is no separate retention cap here.
MIN_FREE_GB = 40


def distill_dir() -> Path:
    """Root of the distillation workspace — always ``<data dir>/distill``.

    A full run needs ~40 GB transient (MLX base + F16 fuse scratch); it follows
    the single root **storage location** (the relocatable data dir, see
    :mod:`memory_core.datadir`) rather than carrying its own knob. ``ESTORMI_DISTILL_DIR``
    (env) still wins as a dev/test override.
    """
    env = os.getenv("ESTORMI_DISTILL_DIR", "").strip()
    if env:
        return Path(env)
    return Path(resolve_data_dir()) / "distill"


def refs_dir() -> Path:
    return distill_dir() / "refs"


def dataset_dir() -> Path:
    return distill_dir() / "dataset"


def adapters_dir() -> Path:
    return distill_dir() / "adapters"


def work_dir() -> Path:
    """Fuse/convert scratch — overridable to a roomier volume."""
    override = os.getenv("ESTORMI_DISTILL_WORK_DIR", "").strip()
    return Path(override) if override else distill_dir() / "work"


def status_path() -> Path:
    return distill_dir() / "status.json"


def read_status() -> dict:
    try:
        data = json.loads(status_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — absent/corrupt = empty status
        return {}


def write_status(**fields) -> dict:
    """Merge ``fields`` into the status file (atomic write) and return it."""
    status = read_status()
    status.update(fields)
    status["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return status
