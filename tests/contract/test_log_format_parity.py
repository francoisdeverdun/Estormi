"""The TS and Swift engine-log parsers must stay byte-for-byte equivalent.

The formatted-log rendering (the briefing "Atelier" and the ingestion log panes)
is implemented twice, once per surface:

  - ``packages/web-ui/src/lib/logFormat.ts``            — CANONICAL (TypeScript)
  - ``apps/estormi-ios/Sources/Metrics/EngineLogFormat.swift`` — Swift mirror

Both tokenise the same raw log lines the Mac emits into the same model, so a
run's log reads identically on macOS/web and on iOS. A true cross-language
codegen here would be high-risk (NSRegularExpression vs JS RegExp semantics,
two rendering stacks); instead we make the duplication *safe*: this contract
test extracts the load-bearing pieces — the line-shape regex patterns, the
named-level mapping, the level-inference keyword regexes, and the source-marker
pattern — from BOTH files by text parsing and asserts they are equivalent.

The point: if the Mac changes a log-line shape and only one side is updated, CI
fails here instead of the two surfaces silently diverging. Swift mirrors TS, so
when this test flags a mismatch, fix the SWIFT side to match the canonical TS.

NOT enforced here: the briefing *phase* markers (``briefingPhases.ts`` PHASES vs
Swift ``briefingMarker``). The Swift side is a deliberate hand-rolled union/
approximation of the per-phase TS regexes (it only needs to highlight phase
lines, not attribute them), so a literal-equality check there would be a false
alarm. That divergence is intentional and documented in EngineLogFormat.swift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
TS = REPO_ROOT / "packages" / "web-ui" / "src" / "lib" / "logFormat.ts"
SWIFT = REPO_ROOT / "apps" / "estormi-ios" / "Sources" / "Metrics" / "EngineLogFormat.swift"


# ── Extractors ──────────────────────────────────────────────────────────────


def _ts_regex_literal(src: str, const_name: str) -> str:
    """Return the body of `const NAME = /…/flags` (between the slashes)."""
    m = re.search(rf"\b{re.escape(const_name)}\s*=\s*/(.*?)/[a-z]*\s*$", src, re.MULTILINE)
    assert m, f"logFormat.ts: could not find regex const `{const_name}`"
    return m.group(1)


def _ts_test_patterns(src: str, func_name: str) -> list[str]:
    """All `/…/.test(...)` regex literals inside a named function body."""
    body = _ts_function_body(src, func_name)
    return re.findall(r"/(.*?)/\.test\(", body)


def _ts_function_body(src: str, func_name: str) -> str:
    """The text from `function NAME(` up to the matching closing brace."""
    start = re.search(rf"\bfunction\s+{re.escape(func_name)}\s*\(", src)
    assert start, f"logFormat.ts: could not find function `{func_name}`"
    i = src.index("{", start.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"logFormat.ts: unbalanced braces in `{func_name}`")


def _swift_raw_patterns(src: str) -> list[str]:
    """Every Swift raw-string pattern literal `#"…"#` in the file."""
    return re.findall(r'#"(.*?)"#', src)


# ── Comparable pieces ────────────────────────────────────────────────────────
#
# Each entry: a label, the TS pattern, and the Swift pattern. The Swift side is
# resolved by membership in the set of all `#"…"#` literals in the file (order-
# independent), which is robust to reordering the Swift declarations.


def _line_shape_patterns() -> dict[str, str]:
    ts = TS.read_text(encoding="utf-8")
    return {
        "engine line": _ts_regex_literal(ts, "ENGINE_RE"),
        "ts/tag line": _ts_regex_literal(ts, "TS_TAG_RE"),
        "run break": _ts_regex_literal(ts, "RUN_BREAK_RE"),
        "source marker": _ts_regex_literal(ts, "SOURCE_MARKER"),
    }


def _infer_level_patterns() -> list[str]:
    ts = TS.read_text(encoding="utf-8")
    pats = _ts_test_patterns(ts, "inferLevel")
    assert len(pats) == 4, f"expected 4 inferLevel regexes in TS, found {pats}"
    return pats


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,ts_pat", list(_line_shape_patterns().items()))
def test_line_shape_pattern_present_in_swift(label: str, ts_pat: str) -> None:
    swift_pats = set(_swift_raw_patterns(SWIFT.read_text(encoding="utf-8")))
    assert ts_pat in swift_pats, (
        f"log-line `{label}` pattern diverged.\n"
        f"  TS (canonical): {ts_pat!r}\n"
        f'  not found among Swift #"…"# patterns: {sorted(swift_pats)}\n'
        f"Update {SWIFT.relative_to(REPO_ROOT)} to match the canonical TS."
    )


@pytest.mark.parametrize("ts_pat", _infer_level_patterns())
def test_infer_level_keyword_pattern_present_in_swift(ts_pat: str) -> None:
    swift_pats = set(_swift_raw_patterns(SWIFT.read_text(encoding="utf-8")))
    assert ts_pat in swift_pats, (
        f"inferLevel keyword regex diverged.\n"
        f"  TS (canonical): {ts_pat!r}\n"
        f'  not found among Swift #"…"# patterns: {sorted(swift_pats)}\n'
        f"Update {SWIFT.relative_to(REPO_ROOT)} inferLevel to match the canonical TS."
    )


def test_named_level_mapping_agrees() -> None:
    """mapNamedLevel: both sides must bucket the same set of raw level words."""
    ts = TS.read_text(encoding="utf-8")
    swift = SWIFT.read_text(encoding="utf-8")

    # Both files spell the same level words (INFO/WARN/WARNING/ERROR/CRITICAL);
    # compare the full recognised set rather than the per-bucket mapping so the
    # check is robust to switch/if styling differences across the two languages.
    ts_level_words = set(re.findall(r"'(INFO|WARN|WARNING|ERROR|CRITICAL)'", ts))
    swift_level_words = set(re.findall(r'"(INFO|WARN|WARNING|ERROR|CRITICAL)"', swift))
    assert ts_level_words == swift_level_words, (
        "mapNamedLevel recognises different level words across surfaces.\n"
        f"  TS:    {sorted(ts_level_words)}\n"
        f"  Swift: {sorted(swift_level_words)}\n"
        f"Update {SWIFT.relative_to(REPO_ROOT)} to match the canonical TS."
    )


def test_swift_mirrors_all_line_shape_and_infer_patterns() -> None:
    """No TS-side pattern is silently dropped on the Swift side (completeness)."""
    expected = set(_line_shape_patterns().values()) | set(_infer_level_patterns())
    swift_pats = set(_swift_raw_patterns(SWIFT.read_text(encoding="utf-8")))
    missing = expected - swift_pats
    assert not missing, (
        f"Swift parser is missing {len(missing)} canonical pattern(s): {sorted(missing)}.\n"
        f"Update {SWIFT.relative_to(REPO_ROOT)} to match {TS.relative_to(REPO_ROOT)}."
    )
