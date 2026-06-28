"""Contract: the committed TS client (schema.d.ts) matches the OpenAPI spec.

``packages/web-ui/src/api/schema.d.ts`` is generated from the committed
``docs/specs/openapi.json`` by ``openapi-typescript`` (``pnpm gen:api``). The
OpenAPI freshness gate (``test_openapi_spec_current.py``) only covers the
app→spec half; this covers the spec→TS half. Without it, a route/model change
that regenerated the spec but not the TS types would drift the client until the
mismatch surfaced at runtime. It re-runs the generator to a temp file and
asserts the committed file is byte-identical.

Skips cleanly when Node / the ``openapi-typescript`` binary is unavailable (the
pure-Python CI lane and a fresh checkout have no ``node_modules``), mirroring
the generator-presence guard in ``test_tokens_swift_current.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

_CI = os.environ.get("CI", "") != ""

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "openapi.json"
SCHEMA_DTS = REPO_ROOT / "packages" / "web-ui" / "src" / "api" / "schema.d.ts"
GENERATOR = REPO_ROOT / "packages" / "web-ui" / "node_modules" / ".bin" / "openapi-typescript"


def test_schema_dts_matches_openapi_spec(tmp_path):
    if not SPEC_PATH.exists():
        assert not _CI, "docs/specs/openapi.json missing in CI"
        pytest.skip("docs/specs/openapi.json not present in this checkout")
    if not SCHEMA_DTS.exists():
        assert not _CI, "schema.d.ts missing in CI"
        pytest.skip("schema.d.ts not present in this checkout")
    if not GENERATOR.exists() or shutil.which("node") is None:
        assert not _CI, "openapi-typescript / node not installed in CI"
        pytest.skip("openapi-typescript / node not installed (run `pnpm install`)")

    out = tmp_path / "schema.d.ts"
    result = subprocess.run(
        [str(GENERATOR), str(SPEC_PATH), "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"openapi-typescript failed:\n{result.stderr}"

    expected = out.read_text(encoding="utf-8")
    current = SCHEMA_DTS.read_text(encoding="utf-8")
    assert current == expected, (
        "packages/web-ui/src/api/schema.d.ts is out of date with "
        "docs/specs/openapi.json — run `make openapi` (or `pnpm --filter "
        "@estormi/web-ui gen:api`) and commit the result."
    )
