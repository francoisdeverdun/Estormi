"""``requirements.lock`` must stay in sync with the floors it is compiled from.

``requirements.lock`` is the reproducible, hash-pinned, full transitive closure
that CI installs (``make lock`` regenerates it via
``uv pip compile packages/estormi_server/requirements.txt tests/requirements-test.txt
--universal --generate-hashes``). Because CI installs the lock — not the loose
floors — a dependency added to a requirements file but never re-locked would be
silently *absent* from every CI job, so the thing CI tests would drift from the
thing contributors declare.

This contract closes that gap from the lock side: every top-level package named
in the dev ``requirements.txt`` and the test ``requirements-test.txt`` must be
present in ``requirements.lock``, and the lock's exact ``==`` pin must satisfy
the floor's specifier. (The complementary dev ⊆ bundle check lives in
``test_requirements_consistency.py``.) Names compare on their PEP 503 normalized
form; extras are ignored — ``qdrant-client[local]`` matches ``qdrant-client``.

A second contract guards the third edge of the dependency triangle: the macOS
bundle (``requirements-bundle.txt``) must pin the SAME versions as the lock for
every package they share — otherwise CI (and the committed OpenAPI spec) validate
one set of versions while the shipped app runs another. The lock is universal, so
its entries are marker-filtered to the bundle's target environment (macOS
arm64 / bundled CPython 3.12) before comparing; packages only in the bundle
(pyobjc, yt-dlp, …) or only in the lock (test-only deps) are out of scope.

When either fails the fix is almost always ``make lock`` (then sync the bundle
pin and commit both).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_REQ = REPO_ROOT / "packages" / "estormi_server" / "requirements.txt"
TEST_REQ = REPO_ROOT / "tests" / "requirements-test.txt"
LOCK = REPO_ROOT / "requirements" / "requirements.lock"
BUNDLE_REQ = REPO_ROOT / "requirements" / "requirements-bundle.txt"

# A locked entry starts at column 0 as ``name==version`` (hashes/markers follow
# on indented continuation lines, comments start with ``#``).
_LOCK_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
# Same, but capturing the optional environment marker after ``;`` (the
# ``--universal`` lock pins e.g. two numpy versions split on python_full_version).
_LOCK_PIN_MARKER_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)(?:\s*;\s*([^\\]+?))?\s*\\?\s*$"
)

# The packaged app's runtime: Apple Silicon macOS with the bundled
# python-build-standalone CPython 3.12 (see Makefile PYTHON_STANDALONE_URL).
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


def _parse_floors(path: Path) -> dict[str, Requirement]:
    reqs: dict[str, Requirement] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        reqs[canonicalize_name(req.name)] = req
    return reqs


def _parse_lock(path: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _LOCK_PIN_RE.match(raw)
        if m:
            pins[canonicalize_name(m.group(1))] = Version(m.group(2))
    return pins


def test_lock_covers_and_satisfies_all_floors():
    floors = {**_parse_floors(DEV_REQ), **_parse_floors(TEST_REQ)}
    lock = _parse_lock(LOCK)
    assert lock, "requirements.lock parsed to zero pins — wrong format or path?"

    missing: list[str] = []
    violations: list[str] = []

    for name, req in sorted(floors.items()):
        pinned = lock.get(name)
        if pinned is None:
            missing.append(f"  {req.name} ({req.specifier or '*'})")
            continue
        if not req.specifier.contains(pinned, prereleases=True):
            violations.append(
                f"  {req.name}: lock pins =={pinned} but floor requires {req.specifier}"
            )

    parts: list[str] = []
    if missing:
        parts.append(
            "Floors absent from requirements.lock — run `make lock` and commit:\n"
            + "\n".join(missing)
        )
    if violations:
        parts.append(
            "Lock pins that violate a floor — run `make lock` and commit:\n" + "\n".join(violations)
        )
    assert not parts, "\n\n".join(parts)


def _parse_lock_for_bundle_target(path: Path) -> dict[str, Version]:
    """Lock pins effective on the bundle's target platform (macOS arm64, py3.12).

    The ``--universal`` lock can pin one package at several versions split by
    environment markers; only the entry whose marker matches the packaged app's
    runtime is the version the bundle must agree with.
    """
    pins: dict[str, Version] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _LOCK_PIN_MARKER_RE.match(raw)
        if not m:
            continue
        marker = m.group(3)
        if marker and not Marker(marker.strip()).evaluate(_BUNDLE_TARGET_ENV):
            continue
        pins[canonicalize_name(m.group(1))] = Version(m.group(2))
    return pins


def _parse_bundle(path: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)", line)
        if m:
            pins[canonicalize_name(m.group(1))] = Version(m.group(2))
    return pins


def test_bundle_pins_match_lock_on_shared_packages():
    """CI validates the lock's versions; the shipped app runs the bundle's.

    For every package both files pin, the versions must be identical — a
    divergence means the packaged macOS app ships dependency versions that no
    CI job (and no committed OpenAPI spec) ever exercised.
    """
    lock = _parse_lock_for_bundle_target(LOCK)
    bundle = _parse_bundle(BUNDLE_REQ)
    assert lock and bundle, "lock or bundle parsed to zero pins — wrong format or path?"

    diverged = [
        f"  {name}: bundle =={bundle[name]} but lock =={lock[name]}"
        for name in sorted(set(lock) & set(bundle))
        if bundle[name] != lock[name]
    ]
    assert not diverged, (
        "requirements-bundle.txt pins diverge from requirements.lock — the app would "
        "ship versions CI never tested. Sync the bundle pin(s) to the lock (or re-run "
        "`make lock` if the floor moved) and commit both:\n" + "\n".join(diverged)
    )
