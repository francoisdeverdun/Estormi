"""Contract: the shell → ``python -m`` ingest seam stays in sync.

The Apple-connector ingest bodies used to live in ``python3 - <<'PYEOF'``
heredocs; they were extracted into importable modules
(``estormi_ingestion.<source>.ingest``) and the ``watch_and_ingest.sh`` scripts
now invoke them as ``"$PY" -m estormi_ingestion.<source>.ingest <positional…>``,
read back by index in each module's ``main(argv)``.

The module unit tests (``tests/.../test_apple_connector_ingest.py``) call
``main()`` with a hand-written argv that *mirrors* the shell by hand — so if a
shell positional is reordered, added, or dropped, every unit test stays green
while the 03:00 run crashes or mis-chunks. That is the exact "never executed by
the test suite" regression class the extraction was meant to close. This test
re-closes it from the shell side: it statically parses each ``-m`` invocation,
counts the positionals the shell passes, and asserts that count both matches the
pinned contract and covers the highest ``argv[N]`` index the module reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

INGESTION = Path(__file__).resolve().parents[2] / "packages" / "estormi_ingestion"

# (shell script, invoked module, module file, expected positional count).
# The count is the number of positional args the .sh passes after the module
# name. Bumping it is a deliberate review checkpoint — update here and in the
# module's main(argv) together.
SEAMS = [
    ("imessage/watch_and_ingest.sh", "estormi_ingestion.imessage.ingest", "imessage/ingest.py", 6),
    (
        "apple_mail/watch_and_ingest.sh",
        "estormi_ingestion.apple_mail.ingest",
        "apple_mail/ingest.py",
        6,
    ),
    (
        "apple_notes/watch_and_ingest.sh",
        "estormi_ingestion.apple_notes.ingest",
        "apple_notes/ingest.py",
        5,
    ),
    (
        "reminders/watch_and_ingest.sh",
        "estormi_ingestion.reminders.ingest",
        "reminders/ingest.py",
        4,
    ),
    (
        "reminders/watch_and_ingest.sh",
        "estormi_ingestion.reminders.mark_complete",
        "reminders/mark_complete.py",
        2,
    ),
]

_SHELL_ARG_RE = re.compile(r'"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"')


def _logical_command(sh_text: str, module: str) -> str:
    """The full ``-m <module> …`` command, joining ``\\`` continuation lines."""
    lines = sh_text.splitlines()
    needle = f"-m {module}"
    for i, line in enumerate(lines):
        if needle in line:
            # Join continuation lines so the positionals (often on the next
            # line) are included in one logical command.
            buf = [line]
            j = i
            while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                buf.append(lines[j])
            return " ".join(seg.rstrip().rstrip("\\") for seg in buf)
    raise AssertionError(f"{needle!r} not found")


@pytest.mark.parametrize("sh_rel,module,mod_rel,expected", SEAMS, ids=[s[1] for s in SEAMS])
def test_shell_positional_count_matches_module(sh_rel, module, mod_rel, expected):
    cmd = _logical_command((INGESTION / sh_rel).read_text(encoding="utf-8"), module)
    # Everything after the module name is the positional argument list.
    after = cmd.split(module, 1)[1]
    n_shell = len(_SHELL_ARG_RE.findall(after))
    assert n_shell == expected, (
        f"{sh_rel}: passes {n_shell} positionals to `-m {module}`, contract expects "
        f"{expected}. If this is intentional, update SEAMS and the module's main(argv)."
    )

    mod_text = (INGESTION / mod_rel).read_text(encoding="utf-8")
    # Highest argv[N] index the module reads (argv[0] is the module name).
    indices = [int(m) for m in re.findall(r"argv\[(\d+)\]", mod_text)]
    max_index = max(indices) if indices else 0
    assert max_index <= n_shell, (
        f"{mod_rel} reads argv[{max_index}] but {sh_rel} only passes {n_shell} "
        f"positionals — the module would IndexError at runtime."
    )
