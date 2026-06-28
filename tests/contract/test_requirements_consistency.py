"""The two requirements files must stay mutually compatible.

Estormi keeps its Python dependencies in two files *by design* (see the
rationale comment at the top of ``pyproject.toml``):

  1. ``estormi_server/requirements.txt`` — the dev / contributor install. Loose
     ``>=`` floors so a fresh ``pip install -r`` picks compatible latest
     versions. Drives ``scripts/setup.sh``, CI lint+test, and pre-commit.
  2. ``requirements-bundle.txt`` — the packaged macOS app. Exact ``==`` pins for
     every transitive dep so two builds of the same release ship identical
     bytes. Drives ``make bundle`` and the embedded interpreter.

Nothing derives one from the other, so nothing guarantees they agree. A drift
ships silently: if the dev floor moves to ``fastapi>=0.111`` while the bundle
still pins ``fastapi==0.110``, the two files contradict each other and the
packaged app violates the contract the dev environment was tested against.

This contract closes that gap. For every top-level package named in the dev
``requirements.txt`` it asserts the package is ALSO present in the bundle, and
that the bundle's exact pin SATISFIES the dev specifier (e.g. dev ``>=0.111.0``
+ bundle ``==0.111.4`` ⇒ OK; bundle ``==0.110.0`` ⇒ FAIL). Names are compared
on their PEP 503 normalized form (case-insensitive, ``_``/``-`` folded) and
extras are ignored — ``uvicorn[standard]`` matches ``uvicorn``.

Known limitation: this only verifies the dev set is a compatible *subset* of
the bundle (dev ⊆ bundle). It does NOT verify the bundle is a complete,
internally-consistent transitive closure — that is pip's job at ``make bundle``
time. Deriving the bundle from a real lockfile (pip-compile / uv) is a separate
product decision and is deferred.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_REQ = REPO_ROOT / "packages" / "estormi_server" / "requirements.txt"
BUNDLE_REQ = REPO_ROOT / "requirements" / "requirements-bundle.txt"

# Operators that pin a single exact version in the bundle.
_EXACT_OPS = {"==", "==="}


def _parse(path: Path) -> dict[str, Requirement]:
    """Map normalized package name → parsed Requirement for one file.

    Strips comments (full-line and trailing) and blank lines, drops the
    ``--hash=sha256:`` continuation lines that hash-pin the bundle, and trims a
    trailing ``\\`` line-continuation off a pin. Each remaining requirement line
    is PEP 508, so ``packaging`` handles extras and environment markers.
    """
    reqs: dict[str, Requirement] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("--hash="):
            continue
        line = line.rstrip("\\").strip()  # a hashed bundle pin ends with " \"
        if not line:
            continue
        req = Requirement(line)
        reqs[canonicalize_name(req.name)] = req
    return reqs


def _exact_pin(req: Requirement) -> Version | None:
    """The single ``==`` version a bundle requirement pins, or None."""
    for spec in req.specifier:
        if spec.operator in _EXACT_OPS:
            return Version(spec.version)
    return None


def test_dev_floors_satisfied_by_bundle_pins():
    dev = _parse(DEV_REQ)
    bundle = _parse(BUNDLE_REQ)

    missing: list[str] = []
    violations: list[str] = []

    for name, dev_req in sorted(dev.items()):
        bundle_req = bundle.get(name)
        if bundle_req is None:
            missing.append(f"  {dev_req.name} (dev {dev_req.specifier or '*'})")
            continue
        pinned = _exact_pin(bundle_req)
        if pinned is None:
            violations.append(f"  {dev_req.name}: bundle entry '{bundle_req}' is not == pinned")
            continue
        # prereleases=True so a bundle pin like 1.0.0rc1 is still evaluated
        # against the floor rather than silently excluded.
        if not dev_req.specifier.contains(pinned, prereleases=True):
            violations.append(
                f"  {dev_req.name}: bundle pins =={pinned} but dev requires {dev_req.specifier}"
            )

    parts: list[str] = []
    if missing:
        parts.append(
            "Dev dependencies absent from requirements-bundle.txt "
            "(add an exact pin):\n" + "\n".join(missing)
        )
    if violations:
        parts.append(
            "Bundle pins that violate the dev floor "
            "(raise the bundle pin or lower the dev floor):\n" + "\n".join(violations)
        )
    assert not parts, "\n\n".join(parts)
