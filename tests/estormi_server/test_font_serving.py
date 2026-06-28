"""Vendored-font HTTP serving — Phase 4.

Estormi vendors its webfonts under ``assets/fonts/`` and
serves them through FastAPI at ``/fonts/*``. The mount is wired up in
``estormi_server/server/static.py``. These tests pin three contracts:

1. The ``.woff2`` binaries we depend on are actually committed to the
   repository (network-free check via ``scripts/vendor_fonts.py``).
2. A GET against ``/fonts/<name>.woff2`` returns 200 with the
   ``font/woff2`` content-type so browsers don't reject the file.
3. The response carries the long-lived immutable ``Cache-Control``
   header — these binaries are content-addressed by filename and never
   change at runtime, so we serve them with a year-long cache.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FONTS_DIR = REPO_ROOT / "assets" / "fonts"


def test_vendored_fonts_present_on_disk() -> None:
    """``scripts/vendor_fonts.py --check`` is offline and authoritative."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "vendor_fonts.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"vendor_fonts.py --check failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# The mount uses a deterministic filename — picked here as the canary
# because every font family is in scope of this single asset.
KNOWN_FONT = "inter-variable.woff2"


async def test_known_font_is_served(client) -> None:
    """``/fonts/<name>.woff2`` returns 200 with ``font/woff2``.

    Uses the in-process ASGI client so the test never opens a socket.
    """
    assert (FONTS_DIR / KNOWN_FONT).is_file(), (
        "fixture font missing — run `python3 scripts/vendor_fonts.py`"
    )

    resp = await client.get(f"/fonts/{KNOWN_FONT}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "font/woff2"
    # Binary should be exactly what we have on disk.
    assert len(resp.content) == (FONTS_DIR / KNOWN_FONT).stat().st_size


async def test_font_response_carries_immutable_cache_header(client) -> None:
    """Long-lived cache so browsers re-use the binary across sessions."""
    resp = await client.get(f"/fonts/{KNOWN_FONT}")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    # We don't pin the exact string so a future tweak (e.g. adding
    # ``stale-while-revalidate``) doesn't break the test — only the
    # invariants that make webfonts cacheable.
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control


async def test_fonts_css_is_served_and_references_local_paths(client) -> None:
    """The CSS lives next to the binaries so the SPA can resolve both
    through a single mount. It MUST reference ``/fonts/...`` paths
    (never ``fonts.gstatic.com``) — otherwise the app would still
    phone home at runtime."""
    resp = await client.get("/fonts/fonts.css")
    assert resp.status_code == 200
    body = resp.text
    assert "fonts.gstatic.com" not in body
    assert "fonts.googleapis.com" not in body
    assert "/fonts/inter-variable.woff2" in body
