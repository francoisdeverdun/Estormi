"""Apply the Estormi app icon as the Finder icon of the iCloud vault folder.

So the user can spot the right folder at a glance in Finder and in the
iOS Files app's iCloud Drive browser. macOS-only; no-ops elsewhere or when
``pyobjc-framework-Cocoa`` is unavailable.

NSWorkspace.setIcon writes a hidden ``Icon\\r`` file carrying the icon as a
resource fork and flips the folder's ``kHasCustomIcon`` Finder bit — the
canonical Apple-supported path, which Finder, Spotlight and iCloud Drive
all understand.
"""

from __future__ import annotations

import platform
from pathlib import Path

import structlog

log = structlog.get_logger()


def find_app_icon() -> Path | None:
    """Locate the Estormi ``icon.icns`` in source-checkout and packaged-bundle
    layouts. Returns ``None`` when neither candidate resolves."""
    here = Path(__file__).resolve()
    # estormi_ingestion/shared/delivery/macos_folder_icon.py → ../../../.. = repo root in source
    # checkout, or Resources/_up_/ in the Tauri bundle.
    repo = here.parent.parent.parent.parent
    candidates = [
        repo / "apps" / "estormi-macos" / "icons" / "icon.icns",
        # Packaged bundle: Tauri places the bundle.icon at
        # ``<App>.app/Contents/Resources/icon.icns``; ``repo`` is the sibling
        # ``Resources/_up_`` dir, so its parent is ``Resources``.
        repo.parent / "icon.icns",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def set_folder_icon(folder: Path, icon_icns: Path) -> bool:
    """Apply ``icon_icns`` as ``folder``'s Finder icon. Returns True on
    success, False on every failure mode (non-Darwin, missing inputs,
    PyObjC unavailable, NSWorkspace refused). Never raises."""
    if platform.system() != "Darwin":
        return False
    if not folder.is_dir() or not icon_icns.is_file():
        return False
    try:
        from AppKit import NSImage, NSWorkspace
    except Exception:
        log.debug("AppKit unavailable; skipping folder icon")
        return False
    try:
        image = NSImage.alloc().initWithContentsOfFile_(str(icon_icns))
        if image is None:
            return False
        ok = NSWorkspace.sharedWorkspace().setIcon_forFile_options_(image, str(folder), 0)
        return bool(ok)
    except Exception:
        log.exception("setIcon_forFile failed for %s", folder)
        return False
