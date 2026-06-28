#!/usr/bin/env python3
"""Generate a download badge SVG from the latest git tag.

Usage:
    python scripts/version_badge.py assets/badges/version.svg
    python scripts/version_badge.py assets/badges/version.svg v1.4  # explicit version
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape


def _latest_tag() -> str:
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return "dev"


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(
            "usage: version_badge.py assets/badges/version.svg [vX.Y]",
            file=sys.stderr,
        )
        return 2

    target = Path(sys.argv[1])
    version = sys.argv[2] if len(sys.argv) == 3 else _latest_tag()

    left_label = "Download for macOS"
    right_label = version

    # Fixed panel widths — independent of font-rendering guesswork.
    # textLength on each <text> forces the glyphs to fill the box exactly.
    PAD = 16  # horizontal padding per panel (8 px each side)
    LEFT_W = 148  # wide enough for "Download for macOS" at 11 px bold
    RIGHT_W = max(40, 8 * len(right_label) + PAD)  # scales with version string
    TOTAL_W = LEFT_W + RIGHT_W
    H = 28

    left_color = "#11100C"
    right_color = "#C49A3A"
    text_color = "#F4EBDD"

    # Text is centered in each panel; textLength constrains it to fit.
    left_tl = LEFT_W - PAD
    right_tl = RIGHT_W - PAD
    lx = LEFT_W / 2
    rx = LEFT_W + RIGHT_W / 2

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{TOTAL_W}" height="{H}" role="img" aria-label="Download for macOS: {escape(version)}">
  <title>Download for macOS: {escape(version)}</title>
  <clipPath id="r"><rect width="{TOTAL_W}" height="{H}" rx="4" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{LEFT_W}" height="{H}" fill="{left_color}"/>
    <rect x="{LEFT_W}" width="{RIGHT_W}" height="{H}" fill="{right_color}"/>
  </g>
  <g fill="{text_color}" text-anchor="middle"
     font-family="Verdana,Geneva,DejaVu Sans,sans-serif"
     font-size="11" font-weight="700">
    <text x="{lx}" y="18" textLength="{left_tl}" lengthAdjust="spacing">{escape(left_label.upper())}</text>
    <text x="{rx}" y="18" textLength="{right_tl}" lengthAdjust="spacing">{escape(right_label.upper())}</text>
  </g>
</svg>
"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(svg, encoding="utf-8")
    print(f"wrote {target} ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
