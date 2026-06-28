#!/usr/bin/env python3
"""Generate illuminated drop-cap lettrines (`estormi-cap-<L>-<tone>.svg`).

These mirror the briefing's drop cap — `.b-day > p:first-of-type::first-letter`
in `packages/ui-kit/src/briefing.css`: a solid-gold Cinzel capital, floated so
body text wraps around it. GitHub markdown can't run `::first-letter`, so each
letter is emitted as a floated SVG (`<img align="left">`) instead.

Two tones, paired via `<picture>` + `prefers-color-scheme` so the cap reads on
either GitHub theme:
  * `dark`  — bright gold `#dcba8a` (the exact `--or-clair` the briefing uses),
              for dark backgrounds.
  * `light` — deep antique gold `#8a6a30`, for white backgrounds.

Usage:
    python scripts/generate_caps.py E T        # specific letters, both tones
    python scripts/generate_caps.py --all      # A–Z, both tones
"""

from __future__ import annotations

import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"

# Bright gold matches briefing.css `--or-clair`; deep gold keeps contrast on white.
TONES = {"dark": "#dcba8a", "light": "#8a6a30"}

TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 54" '
    'role="img" aria-label="{L}">\n'
    "  <title>Illuminated initial {L}</title>\n"
    '  <text x="28" y="45" text-anchor="middle" '
    "font-family=\"Cinzel, 'Cinzel Decorative', 'Trajan Pro', Georgia, 'Times New Roman', serif\" "
    'font-weight="700" font-size="54" fill="{C}">{L}</text>\n'
    "</svg>\n"
)


def render(letter: str, colour: str) -> str:
    return TEMPLATE.replace("{L}", letter).replace("{C}", colour)


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args:
        print(__doc__)
        return 2
    letters = (
        [chr(c) for c in range(ord("A"), ord("Z") + 1)]
        if args == ["--all"]
        else [a.upper() for a in args]
    )
    for letter in letters:
        if len(letter) != 1 or not ("A" <= letter <= "Z"):
            print(f"skip {letter!r}: not a single A–Z letter")
            continue
        for tone, colour in TONES.items():
            path = OUT_DIR / f"estormi-cap-{letter}-{tone}.svg"
            path.write_text(render(letter, colour), encoding="utf-8")
            print(f"wrote {path.relative_to(OUT_DIR.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
