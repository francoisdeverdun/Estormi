#!/usr/bin/env python3
"""Fail CI/pre-commit when detect-secrets reports findings new to the baseline.

The baseline (``.github/.secrets.baseline``) records all known-safe
historical findings — npm lockfile integrity hashes, TypeScript config
objects whose property names happen to include the word "key", test fixture
strings that look high-entropy, etc. Any finding present in the working tree
but NOT in the baseline is treated as a regression.

To accept a new finding (after verifying it is genuinely safe), regenerate
the baseline:

    detect-secrets scan --force-use-all-plugins \\
        --exclude-files '(^\\.claude/|tests/test_dates_hashes\\.py$|^node_modules/|^\\.venv/|^python/|^apps/estormi-macos/target/|^dist/|^graphify-out/|^coverage\\.json$|^pnpm-lock\\.yaml$|^\\.github/\\.secrets\\.baseline$)' \\
        > .github/.secrets.baseline

and audit the diff — never blindly accept.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".github" / ".secrets.baseline"
EXCLUDE_FILES = (
    r"(^\.claude/|tests/test_dates_hashes\.py$|^node_modules/|^\.venv/|"
    r"^python/|^apps/estormi-macos/target/|^dist/|^graphify-out/|^coverage\.json$|"
    r"^pnpm-lock\.yaml$|^\.github/\.secrets\.baseline$)"
)


def _baseline_keys() -> set[tuple[str, str]]:
    """Return (filename, hashed_secret) pairs from the committed baseline."""
    if not BASELINE_PATH.exists():
        return set()
    try:
        data = json.loads(BASELINE_PATH.read_text())
    except json.JSONDecodeError:
        return set()
    keys: set[tuple[str, str]] = set()
    for filename, findings in (data.get("results") or {}).items():
        for finding in findings:
            secret = finding.get("hashed_secret")
            if secret:
                keys.add((filename, secret))
    return keys


def main() -> int:
    executable = shutil.which("detect-secrets")
    if executable:
        cmd = [executable]
    elif shutil.which("uvx"):
        cmd = ["uvx", "detect-secrets"]
    else:
        print(
            "detect-secrets is not installed and uvx is unavailable — "
            "run `pip install detect-secrets` (or install uv) and retry.",
            file=sys.stderr,
        )
        return 127
    cmd.extend(
        [
            "scan",
            "--force-use-all-plugins",
            "--exclude-files",
            EXCLUDE_FILES,
        ]
    )
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode

    data = json.loads(proc.stdout or "{}")
    results = data.get("results") or {}

    baseline = _baseline_keys()
    new_findings: dict[str, list[dict]] = {}
    for filename, findings in results.items():
        for finding in findings:
            key = (filename, finding.get("hashed_secret"))
            if key in baseline:
                continue
            new_findings.setdefault(filename, []).append(finding)

    if not new_findings:
        return 0

    print(
        "detect-secrets: new findings not in .github/.secrets.baseline:",
        file=sys.stderr,
    )
    for filename, findings in sorted(new_findings.items()):
        for finding in findings:
            line = finding.get("line_number", "?")
            kind = finding.get("type", "secret")
            print(f"  - {filename}:{line}: {kind}", file=sys.stderr)
    print(
        "\nIf any of these are confirmed false positives, run the command in "
        "scripts/detect_secrets_gate.py's docstring to refresh the baseline.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
