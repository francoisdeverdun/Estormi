#!/usr/bin/env python3
"""Sync ``requirements-bundle.txt`` pin *versions* to ``requirements.lock``.

The bundle is a curated, hash-pinned pin list installed into the shipped macOS
app; the lock is the resolved, hash-pinned closure CI tests. A contract test
(``tests/contract/test_requirements_lock_consistency.py``) requires the two to
agree on every shared package for the bundle's runtime target (macOS arm64,
py3.12) — otherwise the app ships versions CI never exercised.

Dependabot bumps the flat bundle directly and cannot re-resolve the lock, so its
group PRs leave the two diverged (and sometimes pin a version the resolver would
reject, e.g. ``numpy`` past ``mistral-common``'s ``<2.4`` cap). This script is
the "derive the bundle from the lock" half of ``make relock``: after ``make
lock`` regenerates the lock from the floors, it rewrites each shared bundle pin
to the lock's marker-resolved version. Packages absent from the lock (the
macOS-only ``pyobjc-*`` / ``yt-dlp`` pins) are left untouched.

It only edits the version on the pin line; ``scripts/lock_bundle_hashes.py``
then refreshes the hashes. Run via ``make relock`` (never standalone in CI).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "requirements" / "requirements.lock"
BUNDLE = REPO / "requirements" / "requirements-bundle.txt"

# The bundle's runtime target — must match _BUNDLE_TARGET_ENV in
# tests/contract/test_requirements_lock_consistency.py so this script and the
# contract test resolve the ``--universal`` lock's marker-split pins identically.
_BUNDLE_TARGET_ENV = {
    "sys_platform": "darwin",
    "platform_machine": "arm64",
    "platform_system": "Darwin",
    "os_name": "posix",
    "python_version": "3.12",
    "python_full_version": "3.12.10",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
}

_LOCK_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*(.*?))?\s*\\?\s*$")
_BUNDLE_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;\\]+)(\s*\\?\s*)$")


def lock_versions() -> dict[str, str]:
    """Marker-resolved ``canonical name -> version`` for the bundle target."""
    pins: dict[str, str] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        m = _LOCK_PIN.match(raw)
        if not m:
            continue
        marker = m.group(3)
        if marker and not Marker(marker.strip()).evaluate(_BUNDLE_TARGET_ENV):
            continue
        pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def main() -> int:
    lock = lock_versions()
    if not lock:
        print("sync_bundle_pins: lock parsed to zero pins — wrong path/format?", file=sys.stderr)
        return 2
    lines = BUNDLE.read_text(encoding="utf-8").split("\n")
    changed: list[str] = []
    for i, line in enumerate(lines):
        m = _BUNDLE_PIN.match(line)
        if not m:
            continue
        canon = canonicalize_name(m.group(1))
        want = lock.get(canon)
        if want and want != m.group(3):
            lines[i] = f"{m.group(1)}{m.group(2) or ''}=={want}{m.group(4)}"
            changed.append(f"{m.group(1)}: {m.group(3)} -> {want}")
    if changed:
        BUNDLE.write_text("\n".join(lines), encoding="utf-8")
    print(f"sync_bundle_pins: synced {len(changed)} bundle pin(s) to the lock")
    for c in changed:
        print(f"  {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
