#!/usr/bin/env python3
"""Attach (or refresh) ``--hash=sha256:`` pins on every bundle requirement.

The macOS bundle's embedded interpreter is built by ``pip install --require-hashes
-r requirements/requirements-bundle.txt`` (see ``make/bundle.mk``). Hash-pinning
makes the file's own "two builds must produce identical bytes" promise actually
enforced: a yanked-and-re-uploaded or compromised wheel for a pinned version no
longer installs silently into a signed, distributed app.

This rewrites ``requirements-bundle.txt`` IN PLACE, adding the full set of
sha256 digests (every wheel + sdist) PyPI publishes for each pinned version —
``--require-hashes`` then matches whichever artifact pip selects on the build
host. It NEVER re-resolves the dependency graph (unlike ``uv pip compile``), so
the curated pin set and its explanatory comments are preserved exactly.

Idempotent: run it again after bumping a pin to refresh that pin's hashes.

    python scripts/lock_bundle_hashes.py            # rewrite in place
    python scripts/lock_bundle_hashes.py --check    # CI/contract: fail if stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent / "requirements" / "requirements-bundle.txt"
# A bare ``name==version`` pin at the start of a line (not a continuation / hash
# / comment). Names may carry extras, but the bundle uses none today.
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s#\\]+)\s*$")


def _pypi_sha256s(name: str, version: str) -> list[str]:
    """All sha256 digests PyPI publishes for ``name==version`` (wheels + sdist)."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — fixed PyPI host
        data = json.load(resp)
    digests = sorted({u["digests"]["sha256"] for u in data.get("urls", []) if u.get("digests")})
    if not digests:
        raise SystemExit(f"no sha256 artifacts on PyPI for {name}=={version}")
    return digests


def _render(name: str, version: str, hashes: list[str]) -> str:
    lines = [f"{name}=={version} \\"]
    lines += [f"    --hash=sha256:{h} \\" for h in hashes[:-1]]
    lines.append(f"    --hash=sha256:{hashes[-1]}")
    return "\n".join(lines)


def regenerate(text: str) -> str:
    """Return ``text`` with every bare pin expanded to pin + its sha256 hashes.

    Lines already carrying a trailing ``\\`` (a pin that's already hashed) and
    their ``--hash`` continuations are dropped first, then re-emitted fresh, so
    the pass is idempotent and picks up version bumps.
    """
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        # Skip the continuation lines of an already-hashed pin — they are
        # regenerated from the pin line below.
        if stripped.startswith("--hash="):
            continue
        # A pin that was previously hashed ends with " \"; normalise to the bare
        # pin before re-rendering.
        bare = raw[:-1].rstrip() if raw.rstrip().endswith("\\") else raw
        m = _PIN_RE.match(bare.strip())
        if not m:
            out.append(raw)
            continue
        hashes = _pypi_sha256s(m["name"], m["version"])
        out.append(_render(m["name"], m["version"], hashes))
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if the file is not current")
    args = ap.parse_args()

    current = BUNDLE.read_text(encoding="utf-8")
    if args.check:
        # Offline check: every pin must already carry at least one hash. (A full
        # value re-fetch would need the network; the contract test asserts shape.)
        pins, hashed = _audit(current)
        missing = pins - hashed
        if missing:
            print(f"requirements-bundle.txt: {len(missing)} pin(s) lack a hash: {sorted(missing)}")
            return 1
        print(f"requirements-bundle.txt: all {len(pins)} pins hashed.")
        return 0

    BUNDLE.write_text(regenerate(current), encoding="utf-8")
    pins, hashed = _audit(BUNDLE.read_text(encoding="utf-8"))
    print(f"Hashed {len(hashed)}/{len(pins)} bundle pins → {BUNDLE.relative_to(BUNDLE.parents[1])}")
    return 0


def _audit(text: str) -> tuple[set[str], set[str]]:
    """Return (all pin names, names that carry >=1 hash). Shared by --check + tests."""
    pins: set[str] = set()
    hashed: set[str] = set()
    pending: str | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("--hash=") and pending:
            hashed.add(pending)
            continue
        bare = raw.rstrip()[:-1].rstrip() if raw.rstrip().endswith("\\") else raw
        m = _PIN_RE.match(bare.strip())
        if m:
            pending = m["name"].lower()
            pins.add(pending)
        elif not s.startswith("--hash="):
            pending = None
    return pins, hashed


if __name__ == "__main__":
    sys.exit(main())
