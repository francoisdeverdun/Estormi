"""When the SPA bundle is absent, ``/app`` must explain how to build it.

A fresh source checkout has no ``packages/web-ui/dist`` (gitignored). Before the
fix, ``/app`` 404'd with no guidance — exactly what a stranger following the
"Build from source" docs hit. ``register_static_mounts`` now serves an
actionable 503 instead. This pins that behaviour without touching the real
(possibly-built) dist directory.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from estormi_server.server import static

pytestmark = pytest.mark.unit


def test_app_route_explains_how_to_build_when_spa_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist-dist"
    monkeypatch.setattr(static, "_SPA_DIST", missing, raising=True)
    app = FastAPI()
    static.register_static_mounts(app)

    with TestClient(app) as client:
        for path in ("/app", "/app/"):
            resp = client.get(path)
            assert resp.status_code == 503, f"{path} should be 503 when SPA absent"
            assert "make frontend-build" in resp.text
