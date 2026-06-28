#!/usr/bin/env python3
"""Generate the canonical OpenAPI spec from the live FastAPI app.

The FastAPI app (``estormi_server.main:app``) is the single source of truth for
the HTTP wire contract. The public ``/openapi.json`` route is disabled (the
packaged app serves only the SPA), so this script materialises ``app.openapi()``
into a committed artifact — ``docs/specs/openapi.json``. From it the
TypeScript client types are generated (``packages/web-ui/src/api/schema.d.ts``
via ``openapi-typescript``, see ``pnpm gen:api``), and a contract test pins it so
the spec can't silently drift from the routes/models.

Byte-stable (sorted keys, 2-space indent, trailing newline) so running twice is a
no-op and the drift check is exact. The pinned ``requirements.lock`` makes
``app.openapi()`` reproducible across dev and CI (same fastapi/pydantic).

    python scripts/gen_openapi.py            # (re)write the spec
    python scripts/gen_openapi.py --check    # exit 1 if the committed spec is stale

Run via ``make openapi`` / ``make openapi-check``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "openapi.json"


def render() -> str:
    """The current OpenAPI spec as a byte-stable JSON string."""
    pkgs = str(REPO_ROOT / "packages")
    if pkgs not in sys.path:
        sys.path.insert(0, pkgs)  # make `estormi_server` importable when run as a file
    from estormi_server.main import app  # noqa: PLC0415 — import cost only when generating

    spec = app.openapi()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed spec matches the app; exit 1 if stale",
    )
    args = parser.parse_args(argv)

    current = render()
    if args.check:
        existing = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
        if existing != current:
            print(
                f"openapi.json is stale — run `make openapi` and commit ({SPEC_PATH}).",
                file=sys.stderr,
            )
            return 1
        print("openapi.json is up to date.")
        return 0

    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(current, encoding="utf-8")
    print(f"wrote {SPEC_PATH} ({len(current)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
