"""SKILL.md and README.md references stay live.

Two kinds of on-disk guide carry filesystem and Python-module references:
the loadable skills under ``.claude/skills/*/SKILL.md`` (with frontmatter),
and the per-directory ``README.md`` orientation guides scattered through the
tree. When a refactor renames or removes a path, those references go stale
and start handing the next reader (human or agent) dead links.

This test walks every SKILL.md and README.md in the working tree (excluding
build artefacts and ``.claude/worktrees`` checkouts), extracts the two kinds
of references, and asserts each one still resolves.

Filesystem path: must exist on disk.
Python module path: ``importlib.import_module`` must not raise
``ImportError`` — anything else (system-level dependency missing, side
effect failing) is tolerated so the test does not flake on the developer's
laptop.

Known limitation: only extensioned filesystem paths and dotted module
references (whose first segment is in ``KNOWN_TOP_LEVEL_MODULES``) are
validated. Bare symbol names cited in prose without a dotted prefix —
``VaultReader``, ``push_briefing``, ``_corpus_for_source`` — are NOT
checked, so a rename of one of those can leave a SKILL.md silently stale
while this test stays green. A symbol-level check would need to parse each
reference back to its defining module and is out of scope here.
"""

from __future__ import annotations

import importlib
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bundle identifier `app.estormi.local` and similar — these look like dotted
# Python module paths but are not. Keep the heuristic conservative: a name is
# only treated as a module path if its first segment is one we know to be a
# top-level Python package in this repo.
KNOWN_TOP_LEVEL_MODULES = {
    "memory_core",
    "connectors",
    "server",
    "tools",
    "api",
    "pipeline",
    "main",
    "prompt_templates",
    "knowledge",
    "estormi_ingestion",
    "estormi_server",
    "estormi_briefing",
}

# Filesystem path regex: word(/word){1,}.ext — strips trailing punctuation and
# code-block backticks so callers can write `packages/x/y.py` inline.
_FS_PATH_RE = re.compile(r"`?([A-Za-z0-9_./-]+/[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)`?")

# Dotted-module regex: a.b(.c)+ — we filter the first segment against the
# allow-list above so we don't try to import bundle ids etc.
_MODULE_RE = re.compile(r"`?([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*){1,})`?")


def _iter_skill_files() -> list[Path]:
    """Every git-tracked SKILL.md (loadable skills, under .claude/skills/) and
    README.md (per-directory orientation guides). Scanning only tracked files
    keeps generated caches, build output, and worktree clones out of scope for
    free — no exclusion list to maintain."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO_ROOT / rel
        for rel in out.split("\0")
        if rel and Path(rel).name in ("SKILL.md", "README.md")
    ]


def _strip_fenced_blocks(text: str) -> str:
    """Drop ``` fenced ``` segments so example commands don't trigger checks."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _extract_fs_paths(text: str) -> set[str]:
    return {m.group(1) for m in _FS_PATH_RE.finditer(text)}


# Suffixes that mark a token as a filename, not a dotted Python module path.
# (Both the dotted-module regex and the filesystem-path regex can match
# strings like ``connectors.md`` — only the latter is correct.) ``py`` and
# ``sh`` are here because SKILL.md often cites bare filenames inline
# (``tools.py``, ``build.sh``) that the module regex would
# otherwise mistake for a dotted module/attribute path.
_NON_PYTHON_SUFFIXES = {
    "md",
    "py",
    "command",
    "rs",
    "swift",
    "ts",
    "tsx",
    "js",
    "jsx",
    "json",
    "yaml",
    "yml",
    "toml",
    "html",
    "css",
    "scss",
    "sh",
    "lock",
    "txt",
    "csv",
    "db",
    "ini",
    "cfg",
    "plist",
    "xml",
    "sql",
}


# Dotted tokens that look like Python modules but are something else (method
# calls on JS objects, etc.). Empty by default; add as we find false positives.
_MODULE_FALSE_POSITIVES = {
    "server.use",  # JS — MSW handler override (`server.use(http.get(...))`)
}


def _extract_module_paths(text: str) -> set[str]:
    found = {m.group(1) for m in _MODULE_RE.finditer(text)}
    out: set[str] = set()
    for m in found:
        if m in _MODULE_FALSE_POSITIVES:
            continue
        if m.split(".", 1)[0] not in KNOWN_TOP_LEVEL_MODULES:
            continue
        # ``connectors.md`` / ``main.rs`` etc — file extension, not a module.
        if m.rsplit(".", 1)[-1].lower() in _NON_PYTHON_SUFFIXES:
            continue
        out.add(m)
    return out


def _module_ref_resolves(mod: str) -> bool:
    """A dotted token from a SKILL.md is considered live if it imports as a
    module, or if it is ``module.attribute`` where the parent module imports
    and exposes the attribute. SKILL.md files frequently cite functions —
    ``tools.embed_one``, ``server.jobs.enqueue``, ``tools.sqlite_conn`` — not just
    importable modules.

    Side-effect/dependency errors (anything other than ImportError) are
    tolerated: the path itself is real even if importing it on this machine
    has side effects that fail.
    """
    try:
        importlib.import_module(mod)
        return True
    except ImportError:
        pass
    except Exception:
        return True
    if "." not in mod:
        return False
    parent, attr = mod.rsplit(".", 1)
    try:
        parent_mod = importlib.import_module(parent)
    except ImportError:
        return False
    except Exception:
        return True
    return hasattr(parent_mod, attr)


@pytest.fixture(scope="module")
def skill_files() -> list[Path]:
    files = _iter_skill_files()
    assert files, "no SKILL.md/README.md files found — repo layout changed?"
    return files


def _resolve_fs_ref(skill: Path, ref: str) -> Path | None:
    """Try a few common resolution roots for a path written inside a guide.

    SKILL.md / README.md authors variously write paths as:

      * repo-relative — ``memory_core/storage.py``
      * skill-relative — ``src/index.ts`` from ``packages/ui-kit/README.md``
      * dot-dot relative — ``../../docs/connectors.md``

    Return the first candidate that exists, or ``None`` if none do. Absolute
    paths (``/Library/Messages/...``), URL-rooted paths (``/fonts/...``), and
    obvious URLs are returned as ``None`` without checking — they're not file
    references the test should police.
    """
    if ref.startswith(("http://", "https://", "//")):
        return None
    if ref.startswith("/"):  # absolute / URL-rooted — not a repo file
        return None
    # Known limitation: the candidate roots below are tried for EVERY SKILL.md,
    # not anchored per-skill. So a reference that is stale for its own skill can
    # still resolve against another package's root (e.g. an ``api/...`` path
    # validating under packages/web-ui/src even when cited elsewhere). This trades
    # some precision for not having to declare a root per SKILL.md; it bounds
    # false *failures*, at the cost of occasionally missing a genuinely stale path.
    candidates = [
        REPO_ROOT / ref,  # repo-rooted reference
        # The six first-party Python packages live under packages/, so a SKILL.md
        # path like ``estormi_server/server/jobs.py`` resolves there too.
        REPO_ROOT / "packages" / ref,
        skill.parent / ref,  # relative to the SKILL.md
        # Several frontend guides (README.md) write paths relative to an implicit
        # ``src/`` root (e.g. ``api/client.ts`` from packages/web-ui means
        # packages/web-ui/src/api/client.ts). Try that too.
        skill.parent / "src" / ref,
        # Cross-package references commonly elide ``estormi_server/`` (e.g.
        # .claude/skills/web-ui/SKILL.md mentions ``server/static.py`` for the
        # FastAPI mount, which lives at estormi_server/server/static.py).
        REPO_ROOT / "packages" / "estormi_server" / ref,
        # The canonical task-scoped skills live at .claude/skills/<domain>/
        # and cite paths relative to the package they document — e.g. the
        # web-ui skill writes ``api/client.ts`` (packages/web-ui/src/...) and
        # the mobile skill writes ``Sources/Vault/VaultReader.swift``
        # (apps/estormi-ios/...). Try those package roots regardless of where
        # the SKILL.md itself sits.
        REPO_ROOT / "packages" / "web-ui" / "src" / ref,
        REPO_ROOT / "apps" / "estormi-ios" / ref,
    ]
    # ``@estormi/<pkg>/<path>`` workspace import specifiers (the regex strips
    # the leading ``@``) resolve to packages/<pkg>/src/<path>.
    if ref.startswith("estormi/"):
        rest = ref[len("estormi/") :]
        if "/" in rest:
            pkg, sub = rest.split("/", 1)
            candidates.append(REPO_ROOT / "packages" / pkg / "src" / sub)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.exists():
            return resolved
    return None


# Known synthetic examples — references the SKILL.md uses as illustrations
# ("e.g. app/notes.tsx") rather than pointing at a real file. Empty until we
# find a less brittle way to detect "this is a hypothetical, not a path".
KNOWN_EXAMPLE_REFS: dict[str, set[str]] = {}

# Generated, gitignored build artefacts a SKILL.md legitimately points at even
# though they are absent from a fresh checkout (and from CI, which never seeds
# them). The graphify knowledge graph is built on demand and is gitignored (see
# .gitignore), so requiring it to exist on disk would fail this contract on any
# clean clone. Match by path prefix.
GENERATED_PREFIXES = ("graphify-out/", "build/coverage/", "dist/")


def test_skill_md_filesystem_references_exist(skill_files):
    missing: list[tuple[str, str]] = []
    for skill in skill_files:
        rel_skill = str(skill.relative_to(REPO_ROOT))
        ignored = KNOWN_EXAMPLE_REFS.get(rel_skill, set())
        body = _strip_fenced_blocks(skill.read_text(encoding="utf-8"))
        for ref in _extract_fs_paths(body):
            if ref in ignored:
                continue
            if ref.startswith(GENERATED_PREFIXES):
                continue
            if ref.startswith(("http://", "https://", "//")):
                continue
            if ref.startswith("/"):  # absolute / URL-rooted
                continue
            if _resolve_fs_ref(skill, ref) is None:
                missing.append((rel_skill, ref))

    assert not missing, "SKILL.md references point to files that no longer exist:\n" + "\n".join(
        f"  {skill}: {ref}" for skill, ref in missing
    )


def test_skill_md_python_module_references_import(skill_files):
    # Make sure the same sys.path setup the runtime relies on is in place
    # (tests/conftest.py adds the repo root — which makes estormi_server and
    # estormi_ingestion importable — and packages/).
    import sys

    for p in (str(REPO_ROOT), str(REPO_ROOT / "packages")):
        if p not in sys.path:
            sys.path.insert(0, p)

    failures: list[tuple[str, str]] = []
    for skill in skill_files:
        body = _strip_fenced_blocks(skill.read_text(encoding="utf-8"))
        for mod in _extract_module_paths(body):
            if not _module_ref_resolves(mod):
                failures.append((str(skill.relative_to(REPO_ROOT)), mod))

    assert not failures, "SKILL.md references modules/symbols that do not resolve:\n" + "\n".join(
        f"  {skill}: {mod}" for skill, mod in failures
    )
