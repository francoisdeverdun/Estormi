"""Pure helpers behind the settings-overview aggregator.

The ``GET /api/settings/overview`` handler in
:mod:`estormi_server.api.overview` stitches together a dozen I/O probes (DB
counts, model status, keyring token, pipeline summary, …). The genuinely
reusable, side-effect-light pieces — byte formatting, recursive directory
sizing with a TTL cache, and the build-version readout — live here so they can
be unit-tested directly.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent

# ---------------------------------------------------------------------------
# Dir-size cache — keyed by resolved Path, value is (monotonic_expiry, size).
# Plain dict read/write is safe: this is single-process async, no lock needed.
# ---------------------------------------------------------------------------
DIR_SIZE_TTL_SECONDS: float = 60.0
_dir_size_cache: dict[Path, tuple[float, int]] = {}


def fmt_bytes(b: int) -> str:
    """Format bytes as B / KB / MB / GB. Pure helper consumed by the
    settings-overview JSON aggregator and by tests."""
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b / 1024:.1f} KB"
    if b < 1024**3:
        return f"{b / 1024**2:.1f} MB"
    return f"{b / 1024**3:.2f} GB"


def dir_size(p: Path) -> int:
    """Recursive byte count for files; 0 for non-existent paths."""
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


async def cached_dir_size(p: Path) -> int:
    """Return the byte count for *p*, reusing a cached value if younger than
    ``DIR_SIZE_TTL_SECONDS``.  Each path is cached independently (keyed by its
    resolved form).  Recomputation runs in a thread so it never blocks the
    event loop."""
    key = p.resolve()
    entry = _dir_size_cache.get(key)
    if entry is not None:
        expiry, size = entry
        if time.monotonic() < expiry:
            return size
    size = await asyncio.to_thread(dir_size, p)
    _dir_size_cache[key] = (time.monotonic() + DIR_SIZE_TTL_SECONDS, size)
    return size


def read_version() -> str:
    """Best-effort build identifier for the footer.

    ``make build-version`` writes the current git tag (e.g. ``v1.8``) or short
    SHA (e.g. ``d48394c``) to ``packages/estormi_server/build_version.txt`` during
    ``make bundle``. Prefer that, fall back to ``VERSION``, then a hard-coded
    default.
    """
    bvf = ROOT / "packages" / "estormi_server" / "build_version.txt"
    try:
        if bvf.exists():
            v = bvf.read_text().strip()
            if v:
                return v
    except OSError:
        pass
    vf = ROOT / "VERSION"
    try:
        return vf.read_text().strip() if vf.exists() else "1.0.0"
    except OSError:
        return "1.0.0"
