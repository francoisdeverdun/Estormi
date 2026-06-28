"""Contract: every bundled wheel pin carries a sha256 hash and the bundle build
installs with ``--require-hashes``.

The macOS ``.app`` ships an embedded interpreter built from
``requirements/requirements-bundle.txt``. Without hash-pinning, a yanked-and-
re-uploaded or compromised wheel for an already-pinned version would install
silently into the signed, distributed app — defeating the file's own
"two builds must produce identical bytes" guarantee. Regenerate with
``python scripts/lock_bundle_hashes.py`` after bumping any pin.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "requirements" / "requirements-bundle.txt"
BUNDLE_MK = REPO_ROOT / "make" / "bundle.mk"

_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s#\\]+)")


def _pins_with_hash_state() -> list[tuple[str, bool]]:
    """``[(pin_name, has_hash)]`` in file order — a pin "has a hash" when at least
    one ``--hash=sha256:`` continuation line immediately follows it."""
    result: list[tuple[str, bool]] = []
    lines = BUNDLE.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        bare = raw.rstrip()[:-1].rstrip() if raw.rstrip().endswith("\\") else raw
        m = _PIN_RE.match(bare.strip())
        if not m:
            i += 1
            continue
        j = i + 1
        has_hash = False
        while j < len(lines) and lines[j].strip().startswith("--hash="):
            has_hash = True
            j += 1
        result.append((m["name"], has_hash))
        i = j
    return result


def test_every_bundle_pin_is_hash_pinned():
    pins = _pins_with_hash_state()
    assert len(pins) >= 150, f"expected the full bundle pin set, found only {len(pins)}"
    missing = [name for name, ok in pins if not ok]
    assert not missing, (
        f"{len(missing)} bundle pin(s) lack a --hash=sha256: line "
        f"(run `python scripts/lock_bundle_hashes.py`): {missing}"
    )


def test_bundle_build_requires_hashes():
    installs = [
        ln
        for ln in BUNDLE_MK.read_text(encoding="utf-8").splitlines()
        if "requirements-bundle.txt" in ln and "pip install" in ln
    ]
    assert installs, "no bundle pip install line found in make/bundle.mk"
    for ln in installs:
        assert "--require-hashes" in ln, f"bundle install must use --require-hashes: {ln.strip()}"
