#!/usr/bin/env python3
"""Generate README QA badges from pytest collection and coverage JSON."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

# QA layers are defined by pytest markers, not directories — a test belongs to
# a layer wherever its file lives in the tree. Order is the reporting order.
_LAYER_MARKERS = ["unit", "integration", "e2e", "contract"]

# The badge must track the CI coverage gate so it never shows "passing" gold
# while CI fails. Mirror the `--cov-fail-under` floor in the Makefile and
# .github/workflows/test.yml.
COVERAGE_FLOOR = 80


# Brand palette — keep badges on the same ink + burnished-gold scheme as
# the rest of the Estormi brand surface.
_INK_BLACK = "#11100C"
_IVORY = "#F4EBDD"
_BURNISHED_GOLD = "#C49A3A"
_VERMILION = "#A83224"


def _text_width(text: str) -> int:
    return 14 + len(text) * 8


def _coverage_color(percent: float) -> str:
    return _BURNISHED_GOLD if percent >= COVERAGE_FLOOR else _VERMILION


def _count_color(count: int) -> str:
    return _BURNISHED_GOLD if count > 0 else _VERMILION


def _badge(label: str, value: str, color: str) -> str:
    label_display = label.upper()
    value_display = value.upper()
    left_width = _text_width(label_display)
    right_width = _text_width(value_display)
    width = left_width + right_width
    value_text_fill = _IVORY if color == _VERMILION else _INK_BLACK
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="28" role="img" aria-label="{escape(label)}: {escape(value)}">
  <title>{escape(label)}: {escape(value)}</title>
  <clipPath id="r"><rect width="{width}" height="28" rx="4" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{left_width}" height="28" fill="{_INK_BLACK}"/>
    <rect x="{left_width}" width="{right_width}" height="28" fill="{color}"/>
  </g>
  <g text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11" font-weight="700">
    <text x="{left_width / 2}" y="18" fill="{_IVORY}">{escape(label_display)}</text>
    <text x="{left_width + right_width / 2}" y="18" fill="{value_text_fill}">{escape(value_display)}</text>
  </g>
</svg>
"""


def parse_collected_count(output: str) -> int:
    """Parse pytest collection totals across verbose and quiet output styles."""
    patterns = [
        r"collected\s+(\d+)\s+items?",
        r"(\d+)\s+tests?\s+collected",
    ]
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return int(match.group(1))
    raise ValueError("could not find pytest collection count")


def collect_test_count(repo_root: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout)
    return parse_collected_count(proc.stdout)


def discover_active_layers(repo_root: Path) -> list[str]:
    """Return the QA layer markers that have at least one test in the suite."""
    tests_dir = repo_root / "tests"
    if not tests_dir.exists():
        return []
    corpus = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in tests_dir.rglob("test_*.py")
        if "helpers" not in path.parts
    )
    return [marker for marker in _LAYER_MARKERS if f"pytest.mark.{marker}" in corpus]


def read_coverage_percent(path: Path) -> float:
    data = json.loads(path.read_text(encoding="utf-8"))
    return float(data["totals"]["percent_covered"])


def write_metrics(
    coverage_json: Path,
    badges_dir: Path,
    repo_root: Path,
    metrics_json: Path | None = None,
) -> dict:
    coverage_percent = read_coverage_percent(coverage_json)
    test_count = collect_test_count(repo_root)
    layers = discover_active_layers(repo_root)
    layer_value = "+".join(layers) if layers else "none"

    badges_dir.mkdir(parents=True, exist_ok=True)
    (badges_dir / "coverage.svg").write_text(
        _badge("coverage", f"{coverage_percent:.0f}%", _coverage_color(coverage_percent)),
        encoding="utf-8",
    )
    (badges_dir / "tests.svg").write_text(
        _badge("tests", str(test_count), _count_color(test_count)),
        encoding="utf-8",
    )
    (badges_dir / "qa-layers.svg").write_text(
        _badge("qa layers", layer_value, _BURNISHED_GOLD if layers else _VERMILION),
        encoding="utf-8",
    )

    metrics = {
        "coverage_percent": round(coverage_percent, 2),
        "tests": test_count,
        "layers": layers,
    }
    if metrics_json:
        metrics_json.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("badges_dir", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--metrics-json", type=Path)
    args = parser.parse_args()

    metrics = write_metrics(
        args.coverage_json,
        args.badges_dir,
        args.repo_root,
        args.metrics_json,
    )
    print(
        "qa metrics: "
        f"{metrics['tests']} tests, "
        f"{metrics['coverage_percent']:.0f}% coverage, "
        f"layers={'+'.join(metrics['layers']) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
