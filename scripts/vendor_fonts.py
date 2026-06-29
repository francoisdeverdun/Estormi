#!/usr/bin/env python3
"""Vendor Google-hosted webfonts into ``assets/fonts/``.

Estormi is an offline-first local app. Loading fonts from
``fonts.googleapis.com`` on every launch is a privacy and reliability
regression — the wordmark renders in a fallback serif until the network
comes back. This script downloads the exact ``.woff2`` Latin subsets the
design system needs and writes them with deterministic, human-readable
filenames under ``assets/fonts/``.

Most Google fonts are now shipped as **variable** ``.woff2`` files — a
single binary covers every weight in a range. When Google returns the
same URL for multiple requested weights, we save one shared file
(``<slug>-variable.woff2``) and have ``fonts.css`` reference it from
every ``@font-face`` block. That keeps the bundle small without
sacrificing variants.

Usage::

    python3 scripts/vendor_fonts.py             # download into the repo
    python3 scripts/vendor_fonts.py --check     # verify files exist (no net)

Only the Latin subset (``U+0000-00FF``, ...) is shipped — the app is
English + French only. Vietnamese / Cyrillic / Greek subsets would add
hundreds of KB without value.

This is a developer-side utility: it is **not** imported at runtime and
introduces no new package dependency (stdlib only).
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO_ROOT / "assets" / "fonts"

# Modern UA so Google serves the slim ``.woff2`` Latin subset CSS.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

LATIN_UNICODE_RANGE = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, "
    "U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, "
    "U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
)


class FontRequest(NamedTuple):
    """One family worth of weights to vendor."""

    css_family: str  # exactly what Google's URL expects, e.g. "Cinzel"
    file_slug: str  # filename prefix, e.g. "cinzel"
    # list of (italic_flag, weight). italic_flag is 0 or 1.
    variants: List[Tuple[int, int]]


# What the design system actually uses (see ``packages/ui-kit/src/tokens.css``).
# Variable fonts (Cinzel, Inter, EB Garamond, JetBrains Mono) cost one file
# per family regardless of how many weights we declare — declaring extra
# weights here only adds @font-face rules in ``fonts.css``, not bytes on
# disk. Cinzel Decorative is the only static-per-weight family here.
FAMILIES: List[FontRequest] = [
    FontRequest(
        "Cinzel",
        "cinzel",
        [(0, 400), (0, 500), (0, 600), (0, 700), (0, 800), (0, 900)],
    ),
    FontRequest(
        "Cinzel Decorative",
        "cinzel-decorative",
        [(0, 400), (0, 700), (0, 900)],
    ),
    FontRequest(
        "Inter",
        "inter",
        [(0, 300), (0, 400), (0, 500), (0, 600), (0, 700)],
    ),
    FontRequest(
        "EB Garamond",
        "eb-garamond",
        [(0, 400), (0, 500), (0, 600), (1, 400), (1, 500)],
    ),
    FontRequest(
        "JetBrains Mono",
        "jetbrains-mono",
        [(0, 400), (0, 500)],
    ),
]


def _build_css_url(family: FontRequest) -> str:
    """Google's css2 endpoint URL covering every variant we want."""

    if any(it == 1 for it, _ in family.variants):
        # mixed italics — needs the ``ital,wght`` axis. Tuples are joined
        # by ``;`` while the two values inside each tuple are joined by ``,``.
        parts = ";".join(f"{it},{w}" for it, w in sorted(family.variants))
        spec = f"family={family.css_family.replace(' ', '+')}:ital,wght@{parts}"
    else:
        weights = ";".join(str(w) for _, w in sorted(family.variants))
        spec = f"family={family.css_family.replace(' ', '+')}:wght@{weights}"
    return f"https://fonts.googleapis.com/css2?{spec}&display=swap"


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read()


_BLOCK_RE = re.compile(
    r"/\*\s*(?P<subset>[a-z0-9-]+)\s*\*/\s*@font-face\s*\{(?P<body>[^}]+)\}",
    re.IGNORECASE,
)


def _parse_latin_blocks(css: str) -> List[Dict[str, str]]:
    """Pull out the ``/* latin */`` @font-face blocks only.

    Returns a list of dicts with keys ``weight``, ``style``, ``url``.
    """

    out: List[Dict[str, str]] = []
    for match in _BLOCK_RE.finditer(css):
        if match.group("subset").strip().lower() != "latin":
            continue
        body = match.group("body")
        weight = re.search(r"font-weight:\s*(\d+)", body)
        style = re.search(r"font-style:\s*(\w+)", body)
        url = re.search(r"url\((https?://[^)]+\.woff2)\)", body)
        if not (weight and style and url):
            continue
        out.append(
            {
                "weight": weight.group(1),
                "style": style.group(1).strip(),
                "url": url.group(1),
            }
        )
    return out


def _static_name(slug: str, weight: int, italic: bool) -> str:
    """Filename for a static (non-variable) font file."""

    suffix = f"-{weight}-italic" if italic else f"-{weight}"
    return f"{slug}{suffix}.woff2"


def _variable_name(slug: str, italic: bool) -> str:
    """Filename for a variable font shared by every weight in a style."""

    suffix = "-variable-italic" if italic else "-variable"
    return f"{slug}{suffix}.woff2"


def _resolve_family(family: FontRequest) -> Tuple[Dict[str, bytes], List[Dict]]:
    """Fetch one family. Returns ``(files_to_write, css_rule_specs)``.

    Variable fonts (where every requested weight resolves to the same
    Google URL within an italic axis) collapse to a single binary; the
    CSS rules still get one ``@font-face`` block per declared weight so
    the variable axis is exercised at render time.
    """

    css_url = _build_css_url(family)
    sys.stderr.write(f"[vendor-fonts] GET {css_url}\n")
    css = _fetch(css_url).decode("utf-8")
    blocks = _parse_latin_blocks(css)

    # Group blocks by (italic, url) — Google reuses the URL across
    # weights when the font is variable.
    by_variant: Dict[Tuple[int, int], str] = {}
    for block in blocks:
        italic = 1 if block["style"] == "italic" else 0
        weight = int(block["weight"])
        by_variant.setdefault((italic, weight), block["url"])

    wanted = list(family.variants)
    missing = [v for v in wanted if v not in by_variant]
    if missing:
        raise SystemExit(
            f"[vendor-fonts] {family.css_family}: missing latin variants "
            f"{missing}; Google CSS did not include them."
        )

    # Detect variable fonts per italic axis: same URL across every
    # weight in that axis.
    urls_by_italic: Dict[int, set[str]] = {0: set(), 1: set()}
    for italic, weight in wanted:
        urls_by_italic[italic].add(by_variant[(italic, weight)])

    files: Dict[str, bytes] = {}
    rules: List[Dict] = []

    fetched: Dict[str, bytes] = {}

    def _get(url: str) -> bytes:
        if url not in fetched:
            fetched[url] = _fetch(url)
        return fetched[url]

    for italic_axis, urls in urls_by_italic.items():
        if not urls:
            continue
        is_variable = len(urls) == 1
        axis_weights = sorted(w for it, w in wanted if it == italic_axis)
        if is_variable:
            shared_name = _variable_name(family.file_slug, bool(italic_axis))
            shared_url = next(iter(urls))
            files[shared_name] = _get(shared_url)
            for weight in axis_weights:
                rules.append(
                    {
                        "family": family.css_family,
                        "weight": weight,
                        "italic": bool(italic_axis),
                        "filename": shared_name,
                    }
                )
        else:
            for weight in axis_weights:
                name = _static_name(family.file_slug, weight, bool(italic_axis))
                files[name] = _get(by_variant[(italic_axis, weight)])
                rules.append(
                    {
                        "family": family.css_family,
                        "weight": weight,
                        "italic": bool(italic_axis),
                        "filename": name,
                    }
                )

    return files, rules


def render_css(rules: List[Dict]) -> str:
    """Emit the ``@font-face`` CSS that references the local files."""

    lines: List[str] = [
        "/* Estormi — vendored webfonts.",
        " * Generated by scripts/vendor_fonts.py. Hand-edit at your peril:",
        " * re-running the vendor script will rewrite this file.",
        " * Served by FastAPI under /fonts/*.woff2 (see",
        " * estormi_server/server/static.py).",
        " */",
    ]
    for rule in rules:
        style = "italic" if rule["italic"] else "normal"
        lines.extend(
            [
                "@font-face {",
                f"  font-family: '{rule['family']}';",
                f"  font-style: {style};",
                f"  font-weight: {rule['weight']};",
                "  font-display: swap;",
                f"  src: url('/fonts/{rule['filename']}') format('woff2');",
                f"  unicode-range: {LATIN_UNICODE_RANGE};",
                "}",
            ]
        )
    return "\n".join(lines) + "\n"


def download_all(target: Path) -> Tuple[Dict[str, int], List[Dict]]:
    """Download every variant. Returns ``(written_sizes, css_rules)``."""

    target.mkdir(parents=True, exist_ok=True)
    sizes: Dict[str, int] = {}
    all_rules: List[Dict] = []
    for family in FAMILIES:
        files, rules = _resolve_family(family)
        for name, data in files.items():
            (target / name).write_bytes(data)
            sizes[name] = len(data)
            sys.stderr.write(f"  -> {name}  {len(data):>6} bytes\n")
        all_rules.extend(rules)
    return sizes, all_rules


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify that every expected variant has a file on disk.",
    )
    args = parser.parse_args(argv)

    if args.check:
        missing: List[str] = []
        for family in FAMILIES:
            for italic, weight in family.variants:
                static_name = _static_name(family.file_slug, weight, bool(italic))
                variable_name = _variable_name(family.file_slug, bool(italic))
                if (
                    not (FONTS_DIR / static_name).is_file()
                    and not (FONTS_DIR / variable_name).is_file()
                ):
                    missing.append(f"{family.css_family} {weight}" + (" italic" if italic else ""))
        if missing:
            print(
                f"[vendor-fonts] missing {len(missing)} variants: {missing[:5]}",
                file=sys.stderr,
            )
            return 1
        # Also verify the css the SPA serves @font-face from is present;
        # without this an interrupted run (binaries written, css not yet
        # rendered) would pass --check while the runtime 404s on every URL.
        if not (FONTS_DIR / "fonts.css").is_file():
            print(
                f"[vendor-fonts] missing fonts.css under {FONTS_DIR}",
                file=sys.stderr,
            )
            return 1
        print(f"[vendor-fonts] OK — all variants present under {FONTS_DIR}")
        return 0

    sizes, rules = download_all(FONTS_DIR)
    (FONTS_DIR / "fonts.css").write_text(render_css(rules), encoding="utf-8")
    total = sum(sizes.values())
    print(
        f"[vendor-fonts] wrote {len(sizes)} files, total {total} bytes; "
        f"{len(rules)} @font-face rules in fonts.css"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
