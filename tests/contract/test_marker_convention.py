"""Contract: every collected test carries at least one layer marker.

The ``make`` targets select tests by marker, not by directory — ``make
test-unit`` runs ``-m unit``, ``make test-contract`` runs ``-m contract``, and so
on (see ``make/test.mk``). A test with NO layer marker still runs under the full
``make test`` sweep but escapes every ``make test-<layer>`` target, so it can
silently rot outside the layered selection its author assumes covers it.

This walks every item collected for the current pytest session and asserts each
carries at least one of the five layer markers. It is meaningful on a full-suite
run (``make test``, which collects all of ``tests/``); on a narrower invocation
it only sees the items collected there, which is fine — it can never false-fail.

A test may legitimately carry MORE than one layer marker (e.g. a schema check
that is both ``integration`` and ``contract``); the union-by-marker selection
keeps such a test inside every layer it claims, so the invariant is "at least
one", not "exactly one".
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract

# The five layer markers the make test-<layer> targets select on. Kept in sync
# with the markers registered in pyproject.toml [tool.pytest.ini_options].
LAYER_MARKERS = frozenset({"unit", "integration", "e2e", "contract", "performance"})


def test_every_collected_test_has_a_layer_marker(request):
    unmarked: list[str] = []
    for item in request.session.items:
        layers = {m.name for m in item.iter_markers()} & LAYER_MARKERS
        if not layers:
            unmarked.append(item.nodeid)

    assert not unmarked, (
        "every test must carry at least one layer marker "
        f"({', '.join(sorted(LAYER_MARKERS))}) so it is selectable by the "
        "make test-<layer> targets — these are unmarked:\n"
        + "\n".join(f"  {nodeid}" for nodeid in unmarked)
    )
