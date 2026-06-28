"""System-resource governor for the engine pipeline.

A cheap read of macOS memory and memory pressure. Consumers:

  - ``llm_local`` sizes the model to the machine from total RAM;
  - ``api/overview`` surfaces a read-only memory-pressure readout to the SPA.

No DB and no heavy deps — just ``sysctl`` and a best-effort log file. On a
non-macOS host every probe degrades to "normal" so the governor never blocks
work on a platform it cannot measure.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from memory_core.settings import resolve_data_dir

NORMAL = "normal"
TIGHT = "tight"
CRITICAL = "critical"


def _log_path() -> Path:
    """Resolve the log path at call time so a relocated data dir (or a test
    override of ``resolve_data_dir``) is honoured — sibling modules
    (embedder, llm_local, tts_local) resolve the data dir the same way.
    Freezing it at import bound the path to whatever the data dir was when
    this module first loaded."""
    return Path(resolve_data_dir()) / "logs" / "resource_guard.log"


# macOS kern.memorystatus_vm_pressure_level — the value the OS itself uses to
# decide when to start terminating apps: 1 normal, 2 warn, 4 critical.
_PRESSURE_LEVELS = {1: NORMAL, 2: TIGHT, 4: CRITICAL}


def _sysctl(name: str) -> str:
    """Return a sysctl value as text, or '' if it cannot be read."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def memory_pressure() -> str:
    """Current macOS memory-pressure tier: ``normal`` / ``tight`` / ``critical``.

    Reads ``kern.memorystatus_vm_pressure_level``. If the probe fails (non-macOS
    host, sysctl missing) it reports ``normal`` — the governor must never wedge
    the pipeline on a platform it cannot measure.
    """
    try:
        return _PRESSURE_LEVELS.get(int(_sysctl("kern.memorystatus_vm_pressure_level")), NORMAL)
    except ValueError:
        return NORMAL


def total_ram_gb() -> float:
    """Physical RAM in GiB (16.0 as a safe fallback)."""
    try:
        return int(_sysctl("hw.memsize")) / (1024**3)
    except ValueError:
        return 16.0


def governor_log(msg: str) -> None:
    """Append a timestamped governor event to ``logs/resource_guard.log``.

    One auditable place for every governor decision — LLM rung choices and the
    like — whichever process (server watcher, briefing engine, LLM loader) made
    it. Best-effort: observability must never break the pipeline.
    """
    try:
        log_path = _log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass  # best-effort: a logging failure must never break the pipeline
