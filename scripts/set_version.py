#!/usr/bin/env python3
"""Set the macOS app version in one command instead of editing N files by hand.

Estormi's app version is declared in four files with nothing deriving one from
another (see tests/contract/test_version_consistency.py):

  1. estormi_server/__init__.py  — __version__ (the Python source of truth)
  2. pyproject.toml              — [project].version
  3. apps/estormi-macos/Cargo.toml      — [package].version
  4. apps/estormi-macos/tauri.conf.json — top-level "version"

Bumping only those four is what shipped a stale spec/README to a public
release: the OpenAPI spec (`docs/specs/openapi.json` embeds `info.version`) and
the README "Latest build" line both silently fell behind. So this also
re-derives every version-bearing artifact that a bump must keep in sync:

  5. apps/estormi-macos/Cargo.lock — the embedded estormi [[package]] version
                                   (a stale lock fails every `cargo --locked`)
  6. README.md                   — the "Latest build — Estormi vX.Y.Z" line
  7. docs/specs/openapi.json      — regenerated via scripts/gen_openapi.py

The README download badge is NOT regenerated here: it's a live shields.io
endpoint reading the latest GitHub release, so it tracks the tag on its own.

The CHANGELOG still needs a curated, dated section, so this prints a reminder
rather than guessing one. A contract test (test_version_consistency.py) pins
these together as the safety net; this is the generator that keeps it green.

    python scripts/set_version.py 1.8.1
    make set-version V=1.8.1

Out of scope (independent version tracks, per the contract test): root
package.json, packages/web-ui & packages/ui-kit, and the iOS MARKETING_VERSION.

Stdlib only (re, subprocess, sys, pathlib) — no new deps.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")


def _sub_once(path: Path, pattern: str, repl: str) -> str:
    """Replace the FIRST match of ``pattern`` in ``path`` and write it back.

    Returns the new text, and asserts exactly one declaration was found so a
    layout change fails loudly instead of silently leaving a file behind.
    """
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit(
            f"error: expected exactly one version declaration in "
            f"{path.relative_to(REPO_ROOT)} (found {n}); pattern={pattern!r}"
        )
    path.write_text(new_text, encoding="utf-8")
    return new_text


def _set_code_declarations(version: str) -> None:
    """Rewrite the four code files that declare the app version."""
    targets = [
        (
            REPO_ROOT / "packages" / "estormi_server" / "__init__.py",
            r'^__version__\s*=\s*"[^"]+"',
            f'__version__ = "{version}"',
        ),
        (
            REPO_ROOT / "pyproject.toml",
            r'^version\s*=\s*"[^"]+"',
            f'version = "{version}"',
        ),
        (
            REPO_ROOT / "apps" / "estormi-macos" / "Cargo.toml",
            r'^version\s*=\s*"[^"]+"',
            f'version = "{version}"',
        ),
        (
            REPO_ROOT / "apps" / "estormi-macos" / "tauri.conf.json",
            r'"version"\s*:\s*"[^"]+"',
            f'"version": "{version}"',
        ),
    ]
    for path, pattern, repl in targets:
        _sub_once(path, pattern, repl)
        print(f"  set {path.relative_to(REPO_ROOT)} -> {version}")


def _update_cargo_lock(version: str) -> None:
    """Sync the workspace package's own version inside the generated Cargo.lock.

    Cargo.lock embeds ``estormi``'s version in its ``[[package]]`` stanza. If it
    falls behind Cargo.toml, every ``cargo --locked`` step (CI ``rust.yml``,
    ``make lint-rust``) fails because the lock no longer matches the manifest —
    the same stale-artifact class as the OpenAPI spec / README above. Cargo would
    rewrite it on the next build, but a bump must leave the tree committable with
    a green ``--locked``, so keep it in lockstep here.
    """
    lock = REPO_ROOT / "apps" / "estormi-macos" / "Cargo.lock"
    if not lock.exists():
        print("  skip apps/estormi-macos/Cargo.lock (not generated yet)")
        return
    text = lock.read_text(encoding="utf-8")
    pattern = r'(\[\[package\]\]\nname = "estormi"\nversion = )"[^"]+"'
    new_text, n = re.subn(pattern, rf'\g<1>"{version}"', text, count=1)
    if n != 1:
        print(
            f"  WARNING: could not find the estormi [[package]] stanza in "
            f"Cargo.lock (found {n}); run `cargo update -p estormi` after the bump.",
            file=sys.stderr,
        )
        return
    lock.write_text(new_text, encoding="utf-8")
    print(f"  set apps/estormi-macos/Cargo.lock (estormi package) -> {version}")


def _update_readme(version: str) -> None:
    """Point the README "Latest build" line at vX.Y.Z."""
    _sub_once(
        REPO_ROOT / "README.md",
        r"(Latest build — \[Estormi )v\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?(\])",
        rf"\g<1>v{version}\g<3>",
    )
    print(f"  set README.md (Latest build) -> v{version}")


def _regenerate(script: str, *args: str) -> None:
    """Run a sibling generator with this same interpreter, surfacing failures."""
    rel = f"scripts/{script}"
    try:
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
            cwd=REPO_ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        # The four code files + README are already written; don't crash the
        # whole bump, but make the un-synced artifact impossible to miss.
        print(
            f"  WARNING: `{rel}` failed (exit {exc.returncode}). The version "
            f"files are set, but this artifact is NOT regenerated — run "
            f"`make openapi` manually.",
            file=sys.stderr,
        )


def set_version(version: str) -> None:
    if not _SEMVER_RE.match(version):
        raise SystemExit(f"error: {version!r} is not a valid X.Y.Z version")

    _set_code_declarations(version)
    _update_cargo_lock(version)
    _update_readme(version)
    _regenerate("gen_openapi.py")

    print(f"✓ app version set to {version}")
    print(
        f"  → next: add a dated `## [{version}] - <date>` section to CHANGELOG.md "
        f"(and its compare link) before tagging."
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/set_version.py X.Y.Z", file=sys.stderr)
        return 2
    set_version(argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
