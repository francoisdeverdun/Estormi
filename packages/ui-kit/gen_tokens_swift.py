#!/usr/bin/env python3
"""Generate the iOS `Tokens.swift` color section from the canonical `tokens.css`.

Design tokens used to be hand-synced in three places (tokens.css, Tokens.swift,
and a BriefingHTMLView fallback `:root`), with a comment pleading they be "kept
in lockstep". This script removes the hand-sync for the Swift side: it parses the
`:root` hex declarations in `packages/ui-kit/src/tokens.css` (the single source
of truth) and regenerates the color section of
`apps/estormi-ios/Sources/Design/Tokens.swift` deterministically.

Only the tokens the Swift app actually references are emitted (see COLOR_MAP) —
the iOS surface uses a curated subset of the full web palette, and emitting the
whole set would create dead `EstormiColor` members. Each entry maps a CSS custom
property to its Swift member name; the hex value is pulled from the CSS so it can
only ever drift in one direction (edit the CSS, run `make tokens`).

The non-color parts of Tokens.swift (EstormiMetric, the `Color(hex:)` extension,
the illuminated-cap gradient whose endpoints are not plain tokens) are hand-
written and preserved verbatim — this script only owns the `EstormiColor` enum's
solid-color members.

Run via `make tokens`. The output is byte-stable: running twice is a no-op.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

UI_KIT = Path(__file__).resolve().parent
REPO_ROOT = UI_KIT.parents[1]
TOKENS_CSS = UI_KIT / "src" / "tokens.css"
TOKENS_SWIFT = REPO_ROOT / "apps" / "estormi-ios" / "Sources" / "Design" / "Tokens.swift"

# CSS custom property -> (Swift member name, section comment or None). Order and
# grouping mirror the existing Tokens.swift so the output is a drop-in. Only
# solid #rrggbb tokens the iOS app references belong here; rgba/gradient/font
# tokens are not Color(hex:) values and stay out.
COLOR_MAP: list[tuple[str, str, str | None]] = [
    ("charbon", "charbon", "Neutrals"),
    ("charbon-3", "charbon3", None),
    ("parchemin", "parchemin", None),
    ("parchemin-os", "parcheminOs", None),
    ("or-ancien", "orAncien", "Gold accent family"),
    ("or-clair", "orClair", None),
    ("or-sombre", "orSombre", None),
    ("pourpre", "pourpre", "Semantic accents"),
    ("pourpre-clair", "pourpreClair", None),
    ("rouge-clair", "rougeClair", None),
    ("enluminure", "enluminure", None),
    ("enluminure-clair", "enluminureClair", None),
    ("vert-sauge", "vertSauge", None),
]

HEADER = """\
import SwiftUI

// GENERATED FROM tokens.css — DO NOT EDIT BY HAND. Run `make tokens`.
//
// The EstormiColor solid-color members below are generated from the canonical
// palette in packages/ui-kit/src/tokens.css by packages/ui-kit/gen_tokens_swift.py.
// Colors are the exact hex values used by the macOS app's CSS variables so the
// briefing renders identically on both surfaces. The capGradient, EstormiMetric,
// and the Color(hex:) extension are hand-maintained and live in this file too.

enum EstormiColor {
"""

# Everything after the generated solid colors is hand-written and preserved as-is.
FOOTER = """\

    // Gold gradient used by the IlluminatedCap letter fill.
    static let capGradient = LinearGradient(
        stops: [
            .init(color: Color(hex: 0xFFE9B8), location: 0.0),
            .init(color: Color(hex: 0xDCBA8A), location: 0.40),
            .init(color: Color(hex: 0xC8A96B), location: 0.75),
            .init(color: Color(hex: 0x8A6A30), location: 1.0),
        ],
        startPoint: .top,
        endPoint: .bottom
    )
}

enum EstormiMetric {
    static let radiusPanel: CGFloat = 12
    static let radiusTight: CGFloat = 4

    enum Motion {
        static let fast: TimeInterval = 0.14
        static let medium: TimeInterval = 0.24
    }
}

extension Color {
    init(hex: UInt32, alpha: Double = 1.0) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self.init(.sRGB, red: r, green: g, blue: b, opacity: alpha)
    }
}
"""

HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


def parse_root_hexes(css: str) -> dict[str, str]:
    """Map every `--name: #rrggbb;` declaration in the first :root block."""
    match = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    if not match:
        raise SystemExit("tokens.css: no :root block found")
    # Strip /* … */ comments so a comment preceding a declaration doesn't get
    # glued onto its name when we split on `:`.
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.DOTALL)
    out: dict[str, str] = {}
    for decl in body.split(";"):
        if ":" not in decl:
            continue
        name, _, value = decl.partition(":")
        name = name.strip()
        value = value.strip()
        if not name.startswith("--"):
            continue
        hx = HEX_RE.match(value)
        if hx:
            out[name[2:]] = hx.group(1).upper()
    return out


def render(hexes: dict[str, str]) -> str:
    lines: list[str] = [HEADER.rstrip("\n")]
    for i, (css_name, swift_name, section) in enumerate(COLOR_MAP):
        if css_name not in hexes:
            raise SystemExit(
                f"tokens.css is missing --{css_name} (referenced by EstormiColor.{swift_name})"
            )
        if section is not None:
            # Blank line before each section except the first (the enum's `{`
            # is the line directly above it).
            if i != 0:
                lines.append("")
            lines.append(f"    // {section}")
        lines.append(f"    static let {swift_name} = Color(hex: 0x{hexes[css_name]})")
    lines.append(FOOTER)
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    css = TOKENS_CSS.read_text(encoding="utf-8")
    generated = render(parse_root_hexes(css))

    check = "--check" in sys.argv[1:]
    current = TOKENS_SWIFT.read_text(encoding="utf-8") if TOKENS_SWIFT.exists() else None
    if check:
        if current != generated:
            print(
                f"{TOKENS_SWIFT.relative_to(REPO_ROOT)} is out of date — run `make tokens`.",
                file=sys.stderr,
            )
            return 1
        print(f"{TOKENS_SWIFT.relative_to(REPO_ROOT)} is up to date.")
        return 0

    if current == generated:
        print(f"{TOKENS_SWIFT.relative_to(REPO_ROOT)} already up to date.")
        return 0
    TOKENS_SWIFT.write_text(generated, encoding="utf-8")
    print(f"Wrote {TOKENS_SWIFT.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
