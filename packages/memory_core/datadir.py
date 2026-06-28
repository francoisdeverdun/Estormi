"""Relocatable data-directory resolution + crash-safe library relocation.

This module is the single source of truth for *where the Estormi library lives*
— the umbrella dir holding ``estormi.db``, the Qdrant vectors, ``models/``,
logs, and the JSON engine state. It is deliberately **pure** (only ``os`` /
``shutil`` / ``json`` / stdlib ``sqlite3``) and imports nothing from
:mod:`memory_core.settings`, because :data:`memory_core.settings.DATA_DIR` is a
module-level constant frozen at import — so the relocation step
(:func:`bootstrap_relocate`) has to run *before* ``settings`` is imported, and
can't depend on it.

Why a pointer file instead of a settings row? The chosen base path can't be a
row in the ``settings`` table: that table lives inside ``estormi.db`` which
lives inside the data dir, so you can't read "where is my database" from inside
the database. The path is therefore stored in a tiny **pointer file** at a fixed
location (the canonical config home, which never moves), exactly how Photos and
Music relocate a library.

Resolution order (see :func:`resolve_data_dir`):
    ``$ESTORMI_DATA_DIR`` env  →  pointer file  →  default (config home).

Relocation is requested by the storage API (it writes a *marker*; it does not
move anything) and performed lazily at the next process start by
:func:`bootstrap_relocate`: copy → verify → flip the pointer → keep the old copy
as a backup. The move is idempotent and survives a crash at any point.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone

# ── Fixed locations ─────────────────────────────────────────────────────────


def config_home() -> str:
    """The canonical, never-relocated config home.

    The pointer + marker always live here, even after the library is moved
    elsewhere. ``ESTORMI_CONFIG_HOME`` overrides it (tests point it at a tmp
    dir); otherwise the macOS Application Support path.
    """
    return os.path.expanduser(
        os.getenv("ESTORMI_CONFIG_HOME") or "~/Library/Application Support/Estormi"
    )


def default_data_dir() -> str:
    """Where the library lives when nothing has relocated it — the config home."""
    return config_home()


def pointer_path() -> str:
    """One-line text file naming the current library dir (absent = default)."""
    return os.path.join(config_home(), "data_dir.path")


def marker_path() -> str:
    """Pending-relocation marker written by the storage API; consumed at boot."""
    return os.path.join(config_home(), ".relocate-pending.json")


# ── Resolution ──────────────────────────────────────────────────────────────


def _volume_ready(path: str) -> bool:
    """True when *path*'s nearest existing ancestor is a writable directory.

    Guards against a pointer aimed at an external volume that is currently
    unplugged: we must never silently create a fresh empty library on a phantom
    mount point.
    """
    p = os.path.abspath(path)
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return os.path.isdir(p) and os.access(p, os.W_OK)


def read_pointer() -> str | None:
    """The relocated library path from the pointer file, or ``None``.

    Returns ``None`` (→ caller falls back to the default) when the pointer is
    absent, empty, or names a path whose volume is missing/unwritable.
    """
    try:
        raw = open(pointer_path(), encoding="utf-8").read().strip()  # noqa: SIM115
    except OSError:
        return None
    if not raw:
        return None
    target = os.path.expanduser(raw)
    if not _volume_ready(target):
        return None
    return target


def resolve_data_dir() -> str:
    """Resolve the Estormi data directory.

    ``$ESTORMI_DATA_DIR`` wins outright (used by tests and to pin the path);
    then the relocation pointer file; otherwise the default config home.
    ``expanduser`` is applied to every source so a ``~`` resolves identically.
    """
    env = os.getenv("ESTORMI_DATA_DIR")
    if env:
        return os.path.expanduser(env)
    pointed = read_pointer()
    if pointed:
        return pointed
    return default_data_dir()


# ── Relocation (crash-safe, idempotent) ──────────────────────────────────────


def _write_pointer(target: str) -> None:
    os.makedirs(config_home(), exist_ok=True)
    tmp = pointer_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(target.rstrip("\n") + "\n")
    os.replace(tmp, pointer_path())


def _clear_marker() -> None:
    try:
        os.remove(marker_path())
    except OSError:
        pass


def write_relocation_marker(src: str, dst: str) -> None:
    """Queue a relocation from *src* to *dst* for the next process start.

    Writes the marker only; the actual move happens in :func:`bootstrap_relocate`
    once the process restarts with no DB handle open.
    """
    os.makedirs(config_home(), exist_ok=True)
    payload = {
        "from": os.path.abspath(os.path.expanduser(src)),
        "to": os.path.abspath(os.path.expanduser(dst)),
        "requestedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = marker_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, marker_path())


def pending_relocation() -> str | None:
    """The queued destination if a relocation marker is present, else ``None``."""
    try:
        spec = json.loads(open(marker_path(), encoding="utf-8").read())  # noqa: SIM115
        return os.path.abspath(os.path.expanduser(str(spec["to"])))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _verify_db(db_path: str) -> bool:
    """Open the copied DB read-only and run ``PRAGMA integrity_check``."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False


def bootstrap_relocate(*, log=None) -> str | None:
    """Perform a queued library relocation, if any. Call once, before importing
    :mod:`memory_core.settings`, at process start.

    Returns the path the library now lives at when a move happened, else
    ``None``. Idempotent and crash-safe: re-running after a crash at any point
    converges to a consistent state.
    """

    def _say(event: str, **kw) -> None:
        if log is not None:
            log.info(event, **kw)

    # An explicit env pin (dev/tests) owns the path — never relocate under it.
    if os.getenv("ESTORMI_DATA_DIR"):
        return None

    try:
        spec = json.loads(open(marker_path(), encoding="utf-8").read())  # noqa: SIM115
    except OSError:
        return None  # no marker → nothing to do
    except ValueError:
        _clear_marker()  # corrupt marker → drop it
        return None

    try:
        src = os.path.abspath(os.path.expanduser(str(spec["from"])))
        dst = os.path.abspath(os.path.expanduser(str(spec["to"])))
    except (KeyError, TypeError):
        _clear_marker()
        return None

    if src == dst:
        _clear_marker()
        return None

    dst_db = os.path.join(dst, "estormi.db")
    src_db = os.path.join(src, "estormi.db")

    # Already-completed move that crashed before clearing the marker: the
    # destination DB is present → just (re)point and finish.
    if os.path.exists(dst_db):
        _write_pointer(dst)
        _clear_marker()
        _say("datadir.relocate.resumed", to=dst)
        return dst

    # Fresh install (no source DB yet): just adopt the destination, no copy.
    if not os.path.exists(src_db):
        os.makedirs(dst, exist_ok=True)
        _write_pointer(dst)
        _clear_marker()
        _say("datadir.relocate.adopted_empty", to=dst)
        return dst

    # Destination volume gone (external disk unplugged): leave the marker in
    # place and stay on the current library. Retry on the next launch.
    if not _volume_ready(dst):
        _say("datadir.relocate.target_unavailable", to=dst)
        return None

    staging = dst.rstrip("/") + ".incoming"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(
            src,
            staging,
            symlinks=True,
            ignore_dangling_symlinks=True,
            # The relocation bookkeeping lives only at the fixed config home —
            # don't carry inert copies into the moved library.
            ignore=shutil.ignore_patterns("data_dir.path", ".relocate-pending.json"),
        )
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _say("datadir.relocate.copy_failed", to=dst, error=str(exc))
        return None

    # Swap staging → dst. When dst doesn't exist this is an atomic rename on the
    # destination's own volume; when the user pre-created an (empty) dst, fold
    # the staged files in.
    if os.path.exists(dst):
        for name in os.listdir(staging):
            shutil.move(os.path.join(staging, name), os.path.join(dst, name))
        shutil.rmtree(staging, ignore_errors=True)
    else:
        os.replace(staging, dst)

    if not _verify_db(dst_db):
        _say("datadir.relocate.verify_failed", to=dst)
        return None  # leave marker so the failure is visible / retried

    _write_pointer(dst)
    _clear_marker()

    # Keep the old library as a timestamped backup so the move is reversible —
    # but only when it isn't the config home (which must keep hosting the
    # pointer). When src IS the default, leave its data in place as the backup.
    if os.path.abspath(src) != os.path.abspath(default_data_dir()):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            os.rename(src, src.rstrip("/") + f".migrated-{stamp}")
        except OSError:
            pass

    _say("datadir.relocate.done", **{"from": src, "to": dst})
    return dst
