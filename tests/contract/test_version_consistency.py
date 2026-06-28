"""The macOS-app version must stay in sync across every file that declares it.

Estormi's macOS app version is declared in four independent places, with
nothing deriving one from another:

  1. ``pyproject.toml`` — ``[project].version``.
  2. ``apps/estormi-macos/Cargo.toml`` — ``[package].version``.
  3. ``apps/estormi-macos/tauri.conf.json`` — top-level ``version``.
  4. ``estormi_server/__init__.py`` — ``__version__`` (the Python source of
     truth: ``main.py``'s FastAPI ``version=…`` and ``mcp_rpc.py``'s MCP
     ``serverInfo.version`` both pass ``__version__`` straight through).

A drift ships silently: the packaged bundle, the HTTP API, and the MCP
handshake can each report a different number, and nothing in the build fails.
This contract pins the four declarations together — change one, change all
four — and additionally guards that the MCP ``serverInfo.version`` keeps
forwarding ``__version__`` rather than reintroducing a hard-coded literal that
could drift.

The generated ``apps/estormi-macos/Cargo.lock`` is pinned alongside them: it
embeds ``estormi``'s own version, and a stale value (the 0.0.2 bump left it at
0.0.1) fails every ``cargo --locked`` step — CI ``rust.yml`` and ``make
lint-rust``. ``scripts/set_version.py`` now rewrites it with the four.

It also pins the two *public-facing* version surfaces to ``__version__`` — the
README "Latest build" line and the download badge — because both fell behind
the four code declarations on the 0.0.2 bump. ``make set-version`` now rewrites
all of them from one command (scripts/set_version.py).

Intentionally OUT of scope (independent version tracks):
  - root ``package.json`` (monorepo tooling version, not the app),
  - ``packages/web-ui/package.json`` and ``packages/ui-kit/package.json``,
  - the iOS ``MARKETING_VERSION``.

Parsed with the stdlib only (``tomllib``, ``json``, ``re``) — no new deps.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _cargo_version() -> str:
    data = tomllib.loads(
        (REPO_ROOT / "apps" / "estormi-macos" / "Cargo.toml").read_text(encoding="utf-8")
    )
    return data["package"]["version"]


def _cargo_lock_version() -> str:
    # Cargo.lock embeds the workspace package's own version in its [[package]]
    # stanza; a stale value fails every `cargo --locked` step (CI + make lint-rust).
    return _regex_version(
        REPO_ROOT / "apps" / "estormi-macos" / "Cargo.lock",
        r'\[\[package\]\]\nname = "estormi"\nversion = "([^"]+)"',
    )


def _tauri_conf_version() -> str:
    data = json.loads(
        (REPO_ROOT / "apps" / "estormi-macos" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    return data["version"]


def _regex_version(path: Path, pattern: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match, f"could not find a version literal in {path.relative_to(REPO_ROOT)}"
    return match.group(1)


def _server_version() -> str:
    # estormi_server/__init__.py: __version__ = "0.0.1"
    # main.py imports this and passes it as FastAPI(version=__version__).
    return _regex_version(
        REPO_ROOT / "packages" / "estormi_server" / "__init__.py",
        r'__version__\s*=\s*"([^"]+)"',
    )


def _readme_latest_build_version() -> str:
    # README.md: > **Latest build — [Estormi v0.0.2](.../releases/latest).**
    return _regex_version(
        REPO_ROOT / "README.md",
        r"Latest build — \[Estormi v([0-9][0-9A-Za-z.\-]*)\]",
    )


def _version_badge_version() -> str:
    # assets/badges/version.svg: aria-label="Download for macOS: v0.0.2"
    return _regex_version(
        REPO_ROOT / "assets" / "badges" / "version.svg",
        r'aria-label="Download for macOS: v([0-9][0-9A-Za-z.\-]*)"',
    )


def test_macos_app_version_in_sync():
    versions = {
        "pyproject.toml ([project].version)": _pyproject_version(),
        "apps/estormi-macos/Cargo.toml ([package].version)": _cargo_version(),
        "apps/estormi-macos/Cargo.lock (estormi [[package]].version)": _cargo_lock_version(),
        "apps/estormi-macos/tauri.conf.json (version)": _tauri_conf_version(),
        "estormi_server/__init__.py (__version__, FastAPI version)": _server_version(),
    }

    distinct = set(versions.values())
    assert len(distinct) == 1, "macOS-app version drift across declaring files:\n" + "\n".join(
        f"  {name}: {value}" for name, value in versions.items()
    )


def test_readme_and_badge_track_app_version():
    """README "Latest build" + the download badge must match ``__version__``.

    These public surfaces fell behind the four code declarations on the 0.0.2
    bump (the spec/badge/README were not regenerated). ``make set-version`` now
    rewrites them together; this pins them so a future bump that forgets one
    fails the gate instead of shipping a stale public version.
    """
    app_version = _server_version()
    surfaces = {
        "README.md (Latest build line)": _readme_latest_build_version(),
        "assets/badges/version.svg (download badge)": _version_badge_version(),
    }
    drift = {name: value for name, value in surfaces.items() if value != app_version}
    assert not drift, (
        f"public version surfaces drifted from estormi_server.__version__ "
        f"({app_version}):\n" + "\n".join(f"  {name}: {value}" for name, value in drift.items())
    )


def test_mcp_server_info_forwards_version_constant():
    """The MCP serverInfo.version must forward ``__version__``, not a literal.

    Hard-coding a string here (the historical bug) lets the MCP handshake report
    a stale version while every other file moves on.
    """
    text = (REPO_ROOT / "packages" / "estormi_server" / "api" / "mcp_rpc.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'"serverInfo"\s*:\s*\{[^}]*?"version"\s*:\s*([^,}\s]+)', text)
    assert match, "could not find serverInfo.version in estormi_server/api/mcp_rpc.py"
    value = match.group(1)
    assert value == "__version__", (
        "MCP serverInfo.version should forward estormi_server.__version__, "
        f"but estormi_server/api/mcp_rpc.py hard-codes {value!r}"
    )
