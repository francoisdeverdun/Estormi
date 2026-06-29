"""Integration tests for the local-LLM model endpoints — ``api/model.py``.

Covers ``GET /api/model/status`` and the EventSource
``GET /api/model/download``. External boundaries (the model file
on disk and ``llm_local.download_model``) are mocked, and the streaming
responses are consumed in full so no background task is left dangling.

The GET download route runs ``while not dl_task.done(): await asyncio.sleep(3)``
to poll download progress. Tests replace that sleep with ``_yield_sleep`` — a
real zero-delay sleep that still yields to the event loop, so the patched
``download_model`` task can actually run. A plain ``AsyncMock`` would *not*
yield, leaving the poll loop spinning forever.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration

# Captured before any patching: ``patch("estormi_server.api.model.asyncio.sleep", ...)``
# mutates the shared ``asyncio`` module, so ``_yield_sleep`` must call the
# original, not whatever is currently installed.
_REAL_ASYNCIO_SLEEP = asyncio.sleep


async def _yield_sleep(_delay: float = 0) -> None:
    """Stand-in for ``asyncio.sleep`` inside the download poll loop.

    Yields control to the event loop (letting the download task progress)
    with no wall-clock delay.
    """
    await _REAL_ASYNCIO_SLEEP(0)


def _sse_events(text: str) -> list[dict]:
    """Parse the ``data:`` payloads out of an SSE response body."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


# ── GET /api/model/status ────────────────────────────────────────────────────


class TestModelStatus:
    async def test_status_model_missing(self, client, tmp_path):
        missing = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
        with (
            patch(
                "memory_core.llm_local._model_path",
                new_callable=AsyncMock,
                return_value=str(missing),
            ),
            patch("memory_core.llm_local.is_loaded", new_callable=AsyncMock, return_value=False),
        ):
            resp = await client.get("/api/model/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["downloaded"] is False
        assert data["loaded"] is False
        assert data["size_bytes"] == 0
        assert data["path"] == str(missing)
        assert data["tier"] == "ministral3-14b"

    async def test_status_model_downloaded(self, client, tmp_path):
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
        model.write_bytes(b"x" * 1234)
        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.is_loaded", new_callable=AsyncMock, return_value=True),
        ):
            resp = await client.get("/api/model/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["downloaded"] is True
        assert data["loaded"] is True
        assert data["size_bytes"] == 1234
        assert data["tier"] == "ministral3-14b"

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("Ministral-3-14B-Instruct-2512-Q4_K_M.gguf", "ministral3-14b"),
        ],
    )
    async def test_status_infers_new_tiers(self, client, tmp_path, filename, expected):
        model = tmp_path / filename
        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.is_loaded", new_callable=AsyncMock, return_value=False),
        ):
            resp = await client.get("/api/model/status")
        assert resp.json()["tier"] == expected


# ── GET /api/model/catalog ─────────────────────────────────────────────────────


class TestModelCatalog:
    async def test_catalog_lists_every_model(self, client):
        from memory_core.llm_local import MODEL_CATALOG

        resp = await client.get("/api/model/catalog")
        assert resp.status_code == 200
        data = resp.json()
        tiers = {m["tier"] for m in data["models"]}
        # local_only tiers are hidden until their on-device file exists —
        # there is nothing the UI could offer to download for them.
        expected = {t for t, meta in MODEL_CATALOG.items() if not meta.get("local_only")}
        assert expected <= tiers <= set(MODEL_CATALOG)
        # Each row carries the fields the UI renders.
        for m in data["models"]:
            assert {
                "tier",
                "label",
                "family",
                "min_ram_gb",
                "expected_bytes",
                "downloaded",
            } <= m.keys()

    async def test_catalog_selection_and_defaults_per_engine(self, client):
        resp = await client.get("/api/model/catalog")
        data = resp.json()
        assert set(data["selection"]) == {"briefing"}
        assert set(data["defaults"]) == {"briefing"}
        # No model_tier settings rows in the test DB → selection equals defaults.
        assert data["defaults"]["briefing"] == "ministral3-14b"


# ── POST /api/model/delete ─────────────────────────────────────────────────────


class TestModelDelete:
    async def test_delete_existing(self, client, tmp_path):
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
        model.write_bytes(b"x" * 1024)
        with (
            patch("memory_core.llm_local.model_file_path", return_value=str(model)),
            patch("memory_core.llm_local.is_loaded", new_callable=AsyncMock, return_value=False),
        ):
            resp = await client.post("/api/model/delete", json={"tier": "ministral3-14b"})
        assert resp.status_code == 200
        assert resp.json() == {"tier": "ministral3-14b", "deleted": True}
        assert not model.exists()

    async def test_delete_absent_is_noop(self, client, tmp_path):
        missing = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
        with (
            patch("memory_core.llm_local.model_file_path", return_value=str(missing)),
            patch("memory_core.llm_local.is_loaded", new_callable=AsyncMock, return_value=False),
        ):
            resp = await client.post("/api/model/delete", json={"tier": "ministral3-14b"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False

    async def test_delete_unknown_tier_rejected(self, client):
        resp = await client.post("/api/model/delete", json={"tier": "bogus"})
        assert resp.status_code == 400


# ── GET /api/model/download (EventSource) ────────────────────────────────────


class TestModelDownloadGet:
    async def test_download_get_already_downloaded(self, client, tmp_path):
        """If the file already exists, the stream short-circuits with 'done'."""
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
        model.write_bytes(b"already here")
        dl = AsyncMock()
        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.download_model", dl),
        ):
            resp = await client.get("/api/model/download")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[0] == {"message": "Starting download…"}
        assert events[-1]["status"] == "done"
        assert "Already downloaded" in events[-1]["message"]
        # Short-circuit means download_model was never invoked.
        dl.assert_not_awaited()

    async def test_download_get_success(self, client, tmp_path):
        """A missing file triggers download_model and ends with a 'done' event."""
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"

        async def fake_download(*_args, **_kwargs):
            return str(model)

        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.model.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/model/download")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[0] == {"message": "Starting download…"}
        assert events[-1] == {"status": "done", "message": f"✓ Ready: {model.name}"}

    async def test_download_get_reports_progress(self, client, tmp_path):
        """While the file grows on disk, a progress event is emitted."""
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"

        async def fake_download(*_args, **_kwargs):
            # Simulate a partially-written file appearing mid-download. A small
            # file is enough — the route reads st_size to compute a percentage.
            model.write_bytes(b"x" * 4096)
            return str(model)

        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.model.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/model/download?tier=ministral3-14b")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        # A progress event fires once the (partial) file is visible on disk.
        progress = [e for e in events if "progress" in e]
        assert progress and progress[0]["progress"] == 0
        assert events[-1]["status"] == "done"

    async def test_download_get_error(self, client, tmp_path):
        """A failing download surfaces as an 'error' event, not a 500."""
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"

        async def boom():
            raise RuntimeError("network down")

        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.download_model", side_effect=boom),
            patch("estormi_server.api.model.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/model/download")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        # The raw exception text ("network down") is logged server-side and the
        # SPA gets a stable opaque code — paths and other internal state in the
        # exception are no longer leaked to the browser.
        assert events[-1] == {
            "status": "error",
            "message": "Error: download_failed (see server logs)",
        }

    async def test_download_get_unknown_tier_falls_back(self, client, tmp_path):
        """An unrecognised ?tier= value falls back to the default expected size."""
        model = tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"

        async def fake_download(*_args, **_kwargs):
            return str(model)

        with (
            patch(
                "memory_core.llm_local._model_path", new_callable=AsyncMock, return_value=str(model)
            ),
            patch("memory_core.llm_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.model.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/model/download?tier=bogus")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[-1]["status"] == "done"

    async def test_download_endpoint_refuses_local_only(self, client):
        resp = await client.get("/api/model/download?tier=ministral3-14b-estormi")
        assert resp.status_code == 400
        assert "local-only" in resp.json()["error"]
