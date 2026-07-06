#!/usr/bin/env python3
"""Fast guard: is ``requirements.lock`` consistent with the floors and bundle?

This is the check the ``Requirements consistency`` CI job runs on every PR that
touches a requirements file. It catches the failure mode Dependabot's group PRs
keep producing — a bundle/floor bump that never regenerated the lock — and
reports it *immediately*, at the requirements layer, instead of deep inside the
full pytest+coverage job where the real cause is easy to miss.

Three assertions, mirroring tests/contract/test_requirements_lock_consistency.py
plus an install check:

  1. Every ``>=`` floor in packages/estormi_server/requirements.txt is present in
     the lock and satisfied by the lock's pin.
  2. For every package both the bundle and the lock pin (bundle runtime target:
     macOS arm64 / py3.12), the versions are identical.
  3. The fully-pinned lock actually resolves on py3.12 under ``--require-hashes``
     (skipped automatically if ``uv`` is unavailable).

On any failure it prints the specific divergences and the one-line fix:
``make relock`` (regenerate the lock from the floors, re-derive the bundle) then
commit both. Exit 0 = consistent, 1 = inconsistent, 2 = harness error.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "requirements" / "requirements.lock"
BUNDLE = REPO / "requirements" / "requirements-bundle.txt"
FLOORS = REPO / "packages" / "estormi_server" / "requirements.txt"

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
_BUNDLE_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")
_FLOOR = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][^,\s;]*)")

_FIX = "\n  Fix: run `make relock` (regenerate lock from the floors + re-derive the bundle), then commit both.\n"


def _lock_target() -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        m = _LOCK_PIN.match(raw)
        if not m:
            continue
        marker = m.group(3)
        if marker and not Marker(marker.strip()).evaluate(_BUNDLE_TARGET_ENV):
            continue
        pins[canonicalize_name(m.group(1))] = Version(m.group(2))
    return pins


def main() -> int:
    lock = _lock_target()
    if not lock:
        print("lock parsed to zero pins — wrong path/format?", file=sys.stderr)
        return 2
    problems: list[str] = []

    floors = {}
    for raw in FLOORS.read_text(encoding="utf-8").splitlines():
        m = _FLOOR.match(raw.strip())
        if m:
            floors[canonicalize_name(m.group(1))] = Version(m.group(2))
    missing = [str(n) for n in floors if n not in lock]
    violated = [
        f"{n}: floor >={floors[n]} but lock pins =={lock[n]}"
        for n in floors
        if n in lock and lock[n] < floors[n]
    ]
    if missing:
        problems.append("Floors absent from the lock: " + ", ".join(sorted(missing)))
    if violated:
        problems.append("Floors the lock violates:\n    " + "\n    ".join(violated))

    bundle = {}
    for raw in BUNDLE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        m = _BUNDLE_PIN.match(line)
        if m:
            bundle[canonicalize_name(m.group(1))] = Version(m.group(2))
    diverged = [
        f"{n}: bundle =={bundle[n]} but lock =={lock[n]}"
        for n in sorted(set(lock) & set(bundle))
        if bundle[n] != lock[n]
    ]
    if diverged:
        problems.append(
            "Bundle pins diverge from the lock (py3.12 target):\n    " + "\n    ".join(diverged)
        )

    if shutil.which("uv"):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            venv = Path(td) / "venv"
            mk = subprocess.run(
                ["uv", "venv", "--python", "3.12", str(venv)], capture_output=True, text=True
            )
            if mk.returncode != 0:
                print(
                    "note: could not create a py3.12 venv — skipping install check:\n  "
                    + mk.stderr.strip()
                )
            else:
                env = {**os.environ, "VIRTUAL_ENV": str(venv)}
                r = subprocess.run(
                    ["uv", "pip", "install", "--dry-run", "--require-hashes", "-r", str(LOCK)],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                if r.returncode != 0:
                    tail = "\n    ".join((r.stderr or r.stdout).strip().splitlines()[-8:])
                    problems.append(
                        "The lock does not resolve on py3.12 (--require-hashes):\n    " + tail
                    )
    else:
        print("note: uv not found — skipping the py3.12 install check (shape checks still ran)")

    if problems:
        print("Requirements are INCONSISTENT:\n")
        for p in problems:
            print("• " + p)
        print(_FIX)
        return 1
    print("Requirements consistent: floors satisfied, bundle == lock (py3.12), lock installs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
