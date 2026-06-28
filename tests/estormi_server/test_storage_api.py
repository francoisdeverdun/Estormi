"""Root storage-location endpoints — read the current dir, queue a relocation.

The move itself is exercised in ``tests/memory_core/test_datadir.py``; here we
cover the API surface: the location shape and the relocate validation guards
(same-as-current, relative, traversal, nesting) plus a happy queue. The suite
pins ``ESTORMI_DATA_DIR``/``ESTORMI_CONFIG_HOME`` to tmp dirs (conftest), so the
marker never touches real data.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestStorageLocation:
    async def test_location_shape(self, client):
        resp = await client.get("/api/storage/location")
        assert resp.status_code == 200
        data = resp.json()
        assert {"dir", "default", "freeGb", "libraryBytes", "pending"} <= data.keys()
        assert isinstance(data["libraryBytes"], int)


class TestStorageRelocate:
    async def test_same_as_current_rejected(self, client):
        current = (await client.get("/api/storage/location")).json()["dir"]
        resp = await client.post("/api/storage/relocate", json={"to": current})
        assert resp.status_code == 400
        assert "already" in resp.json()["error"]

    async def test_relative_path_rejected(self, client):
        resp = await client.post("/api/storage/relocate", json={"to": "relative/dir"})
        assert resp.status_code == 400
        assert "absolute" in resp.json()["error"]

    async def test_traversal_rejected(self, client):
        resp = await client.post("/api/storage/relocate", json={"to": "/Volumes/../etc/x"})
        assert resp.status_code == 400

    async def test_nested_under_current_rejected(self, client):
        current = (await client.get("/api/storage/location")).json()["dir"]
        resp = await client.post("/api/storage/relocate", json={"to": f"{current}/sub"})
        assert resp.status_code == 400
        assert "inside" in resp.json()["error"]

    async def test_empty_rejected(self, client):
        resp = await client.post("/api/storage/relocate", json={"to": "   "})
        assert resp.status_code == 400

    async def test_happy_queue_writes_marker(self, client, tmp_path):
        from memory_core import datadir

        target = str(tmp_path / "newlib")
        resp = await client.post("/api/storage/relocate", json={"to": target})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["willMoveOnRestart"] is True
        assert body["to"] == target
        # The marker is queued (consumed only on the next process start).
        assert datadir.pending_relocation() == target
        datadir._clear_marker()  # don't leak the marker into other tests
