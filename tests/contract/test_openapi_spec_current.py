"""The committed OpenAPI spec must match the live FastAPI app.

``docs/specs/openapi.json`` is the single source of truth for the HTTP wire
contract — the TypeScript client types are generated from it (``pnpm gen:api`` →
``packages/web-ui/src/api/schema.d.ts``). If a route or request/response model
changes without regenerating the spec, the generated TS types drift from the
real API and the mismatch only surfaces at runtime. This contract turns that
into a build-time failure: regenerate the spec from ``app.openapi()`` and assert
it is byte-identical to the committed file.

When this fails the fix is ``make openapi`` (regenerate spec + TS types), then
commit. ``requirements.lock`` pins fastapi/pydantic, so ``app.openapi()`` is
reproducible across dev and CI — this check is exact, not flaky.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "openapi.json"


def test_committed_openapi_matches_app():
    from scripts.gen_openapi import render

    assert SPEC_PATH.exists(), "docs/specs/openapi.json is missing — run `make openapi`."
    committed = SPEC_PATH.read_text(encoding="utf-8")
    current = render()
    assert committed == current, (
        "Committed OpenAPI spec is stale vs the FastAPI app — run `make openapi` and commit "
        "(this regenerates the TS client types too)."
    )
