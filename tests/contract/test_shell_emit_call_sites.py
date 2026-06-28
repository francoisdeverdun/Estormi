"""Contract: every ``post_chunks(...)`` call site binds to the live signature.

The per-source ingest bodies used to live in ``python3 - <<'PYEOF'`` heredocs
inside ``watch_and_ingest.sh``; a signature drift between a heredoc call and
``estormi_ingestion.shared.emit.post_chunks`` was invisible to pytest and
pyright, and once shipped a runtime ``TypeError`` (``post_chunks`` lost a
parameter while two heredocs still passed it, crashing iMessage and Apple Mail
ingestion). The heredocs have since been extracted into importable modules, so
the ``post_chunks`` calls now live in ``.py`` files that pyright already checks —
but this scan stays cheap insurance and still covers any future ``.sh`` that
inlines a call. (The complementary shell→``-m`` *argv* seam — which the
extraction newly introduced — is pinned by ``test_shell_ingest_argv_seam.py``.)

It extracts **every** ``post_chunks(...)`` call across
``packages/estormi_ingestion`` — heredocs (`.sh`) and plain modules (`.py`)
alike — parses each with ``ast``, and binds the argument *shape* (positional
count + keyword names) against ``inspect.signature(post_chunks)``. An unexpected
keyword (``extra=``), a renamed parameter, or a missing required argument fails
here instead of at 03:00 during the nightly ingestion.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from estormi_ingestion.shared.emit import post_chunks

pytestmark = pytest.mark.contract

INGESTION_ROOT = Path(__file__).resolve().parents[2] / "packages" / "estormi_ingestion"
_SIGNATURE = inspect.signature(post_chunks)


def _extract_call(text: str, name_start: int) -> str:
    """Return the ``post_chunks(...)`` text starting at ``name_start``.

    A paren-balanced scan that skips string literals, so nested parens and
    parens inside quoted strings don't end the call early.
    """
    open_paren = text.index("(", name_start)
    depth = 0
    quote: str | None = None
    i = open_paren
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[name_start : i + 1]
        i += 1
    raise ValueError(f"unbalanced parentheses in post_chunks call at offset {name_start}")


def _call_sites() -> list[tuple[Path, str]]:
    """All ``(path, call_text)`` for ``post_chunks(`` across the ingestion tree."""
    sites: list[tuple[Path, str]] = []
    for path in sorted(INGESTION_ROOT.rglob("*.sh")) + sorted(INGESTION_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        start = 0
        while True:
            idx = text.find("post_chunks(", start)
            if idx == -1:
                break
            start = idx + len("post_chunks(")
            if text[max(0, idx - 4) : idx] == "def ":
                continue  # the definition in emit.py, not a call site
            sites.append((path, _extract_call(text, idx)))
    return sites


def test_post_chunks_call_sites_exist():
    """Guard against a silently-empty scan (e.g. the tree moved)."""
    sites = _call_sites()
    # iMessage, Apple Mail, Apple Notes, Reminders heredocs + documents + world.
    assert len(sites) >= 6, f"expected ≥6 post_chunks call sites, found {len(sites)}"


@pytest.mark.contract
def test_every_post_chunks_call_binds_to_the_signature():
    """Each call site must be a valid invocation of the current signature."""
    failures: list[str] = []
    for path, call_text in _call_sites():
        node = ast.parse(call_text, mode="eval").body
        assert isinstance(node, ast.Call), f"{path}: not a call: {call_text!r}"

        # Bind the *shape* (values are irrelevant), so an unexpected keyword,
        # a removed/renamed parameter, or a missing required arg is caught.
        n_positional = sum(1 for a in node.args if not isinstance(a, ast.Starred))
        has_star_args = any(isinstance(a, ast.Starred) for a in node.args)
        kwargs = {kw.arg: object() for kw in node.keywords if kw.arg is not None}
        has_double_star = any(kw.arg is None for kw in node.keywords)
        if has_star_args or has_double_star:
            # A *args/**kwargs spread defeats static arity checking — skip arity
            # but still reject any explicit unknown keyword.
            valid = set(_SIGNATURE.parameters)
            unknown = set(kwargs) - valid
            if unknown:
                failures.append(f"{path}: unknown keyword(s) {sorted(unknown)}")
            continue
        try:
            _SIGNATURE.bind(*([object()] * n_positional), **kwargs)
        except TypeError as exc:
            rel = path.relative_to(INGESTION_ROOT.parents[1])
            failures.append(f"{rel}: {exc}")

    assert not failures, "post_chunks call sites diverge from emit.post_chunks:\n" + "\n".join(
        failures
    )
