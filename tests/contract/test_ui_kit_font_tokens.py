"""Contract: SPA/ui-kit components reference fonts via --font-* tokens.

The design system declares its typefaces once, as CSS custom properties in
``packages/ui-kit/src/tokens.css`` (``--font-display`` Cinzel, ``--font-body``
EB Garamond, ``--font-ui`` Inter, ``--font-mono`` JetBrains Mono). A component
that inlines a raw ``fontFamily: "'Cinzel', serif"`` string-literal forks the
font stack — a later token change (a new display face, a fallback tweak) then
silently misses it. This guard fails on any inline ``fontFamily`` *style-object*
literal so the discipline can't regress.

Allowed: ``fontFamily: 'var(--font-...)'`` (the tokens), the CSS keywords
``inherit`` / ``unset`` / ``initial``, and any non-string value (a JS variable).
The SVG *presentation attribute* form (``fontFamily="..."`` with ``=``, no
colon) is exempt — CSS ``var()`` does not resolve in SVG presentation
attributes, so that one legitimately carries a literal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = [
    REPO_ROOT / "packages" / "ui-kit" / "src",
    REPO_ROOT / "packages" / "web-ui" / "src",
]

# `fontFamily:` (style-object form — note the colon) followed by a quoted string.
_STYLE_FONT_LITERAL = re.compile(r"""fontFamily:\s*(['"])(?P<value>.*?)\1""")
_ALLOWED = {"inherit", "unset", "initial", "revert"}


def _violations() -> list[str]:
    out: list[str] = []
    for root in SCAN_DIRS:
        for path in sorted(root.rglob("*.tsx")) + sorted(root.rglob("*.ts")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                m = _STYLE_FONT_LITERAL.search(line)
                if not m:
                    continue
                value = m.group("value").strip()
                if value.startswith("var(") or value in _ALLOWED:
                    continue
                rel = path.relative_to(REPO_ROOT)
                out.append(f"{rel}:{n}: fontFamily: {m.group(1)}{value}{m.group(1)}")
    return out


def test_no_hardcoded_font_family_literals():
    violations = _violations()
    assert not violations, (
        "Inline fontFamily literals fork the design-system font stack — use a "
        "var(--font-*) token from packages/ui-kit/src/tokens.css instead:\n" + "\n".join(violations)
    )
