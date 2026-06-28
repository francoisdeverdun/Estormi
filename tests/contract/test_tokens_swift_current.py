"""Contract: the committed iOS Tokens.swift matches its generator.

``apps/estormi-ios/Sources/Design/Tokens.swift`` is generated from the canonical
palette in ``packages/ui-kit/src/tokens.css`` by
``packages/ui-kit/gen_tokens_swift.py`` (run via ``make tokens``). The iOS Swift
build is out-of-band from ``make test``, so without this gate a CSS edit (or a
generator change) silently drifts the committed Swift until someone happens to
rebuild the iOS app. This re-runs the generator in-process and asserts the
committed file is byte-identical — the same freshness guarantee
``test_openapi_spec_current.py`` gives the OpenAPI spec.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "packages" / "ui-kit" / "gen_tokens_swift.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_tokens_swift", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tokens_swift_matches_generator():
    if not GENERATOR.exists():
        assert not os.environ.get("CI"), "gen_tokens_swift.py missing in CI"
        pytest.skip("gen_tokens_swift.py not present in this checkout")
    gen = _load_generator()
    if not gen.TOKENS_SWIFT.exists():
        assert not os.environ.get("CI"), "Tokens.swift missing in CI"
        pytest.skip("Tokens.swift not present in this checkout")

    expected = gen.render(gen.parse_root_hexes(gen.TOKENS_CSS.read_text(encoding="utf-8")))
    current = gen.TOKENS_SWIFT.read_text(encoding="utf-8")

    assert current == expected, (
        f"{gen.TOKENS_SWIFT.relative_to(REPO_ROOT)} is out of date with "
        "packages/ui-kit/src/tokens.css — run `make tokens` and commit the result."
    )
