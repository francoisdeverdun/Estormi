"""The EstormiCloud doorbell exit codes are a cross-language contract.

The CloudKit push helper is a Swift command-line tool whose *exit code* is the
only thing the Mac sees; the Python driver branches on it to decide retry vs.
give-up:

  - ``apps/estormi-cloud/Sources/main.swift``                          — Swift `enum Exit`
  - ``packages/estormi_ingestion/shared/delivery/cloudkit_doorbell.py`` — Python driver

If the two sides disagree on what ``2`` (no iCloud account) or ``3`` (network)
mean, the Mac retries a permanent failure forever or gives up on a transient
one. There is no shared build artifact, so this contract pins the numbers by
parsing both files textually (no import — the Python side pulls in CloudKit/
pyobjc, unavailable on the Linux CI runner). The Swift `enum Exit` is the source
of truth; if this flags a mismatch, fix the Python ``_EXIT_*`` constants to match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
_SWIFT = REPO_ROOT / "apps" / "estormi-cloud" / "Sources" / "main.swift"
_PYTHON = (
    REPO_ROOT / "packages" / "estormi_ingestion" / "shared" / "delivery" / "cloudkit_doorbell.py"
)

# Swift `case <name> = <n>`  ↔  Python `_EXIT_<NAME> = <n>` the driver branches on.
_PAIRS = {"ok": "_EXIT_OK", "noAccount": "_EXIT_NO_ACCOUNT", "network": "_EXIT_NETWORK"}


def _swift_exit_cases() -> dict[str, int]:
    text = _SWIFT.read_text(encoding="utf-8")
    block = re.search(r"enum Exit\s*:[^\{]*\{(.*?)\}", text, re.DOTALL)
    assert block, "could not find `enum Exit` in apps/estormi-cloud/Sources/main.swift"
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"case\s+(\w+)\s*=\s*(\d+)", block.group(1))
    }


def _python_exit_constants() -> dict[str, int]:
    text = _PYTHON.read_text(encoding="utf-8")
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^(_EXIT_[A-Z_]+)\s*=\s*(\d+)", text, re.MULTILINE)
    }


def test_doorbell_exit_codes_match_across_swift_and_python():
    swift = _swift_exit_cases()
    python = _python_exit_constants()

    # The Swift enum must still declare the load-bearing cases at their pinned
    # values (the helper's whole contract with the Mac).
    assert swift.get("ok") == 0
    assert swift.get("noAccount") == 2
    assert swift.get("network") == 3

    mismatches = []
    for swift_case, py_const in _PAIRS.items():
        if swift_case not in swift:
            mismatches.append(f"Swift enum Exit is missing `case {swift_case}`")
            continue
        if py_const not in python:
            mismatches.append(
                f"Python is missing `{py_const}` (Swift {swift_case}={swift[swift_case]})"
            )
            continue
        if swift[swift_case] != python[py_const]:
            mismatches.append(
                f"{py_const}={python[py_const]} but Swift {swift_case}={swift[swift_case]}"
            )

    assert not mismatches, "EstormiCloud exit-code contract drift:\n" + "\n".join(mismatches)
