"""TTS (voice) model catalog endpoints — /api/tts/catalog · /delete · /download.

Mirrors test_model.py. External boundaries (the HF snapshot download, the
on-disk model dir) are patched so the suite never hits the network or disk.

The GET download route polls progress with ``while not dl_task.done(): await
asyncio.sleep(3)``. Tests replace that sleep with ``_yield_sleep`` — a real
zero-delay sleep that still yields to the event loop, so the patched
``download_model`` task can actually run to completion. A plain ``AsyncMock``
would not yield, leaving the poll loop spinning forever (and tripping the 60s
ceiling).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration

# Captured before any patching: ``patch("estormi_server.api.tts.asyncio.sleep",
# ...)`` mutates the shared ``asyncio`` module, so ``_yield_sleep`` must call
# the original, not whatever stand-in is currently installed.
_REAL_ASYNCIO_SLEEP = asyncio.sleep


async def _yield_sleep(_delay: float = 0) -> None:
    """Stand-in for ``asyncio.sleep`` inside the download poll loop.

    Yields control to the event loop (letting the threaded download task make
    progress) with no wall-clock delay.
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


class TestTtsCatalog:
    async def test_catalog_lists_voxtral(self, client):
        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=0),
        ):
            resp = await client.get("/api/tts/catalog")
        assert resp.status_code == 200
        body = resp.json()
        assert body["selected"] == "voxtral-4b"
        keys = {m["key"] for m in body["models"]}
        assert "voxtral-4b" in keys
        vox = next(m for m in body["models"] if m["key"] == "voxtral-4b")
        assert {
            "key",
            "label",
            "family",
            "min_ram_gb",
            "expected_bytes",
            "downloaded",
            "size_bytes",
        } <= vox.keys()
        assert vox["downloaded"] is False
        assert vox["size_bytes"] == 0
        assert vox["expected_bytes"] > 0

    async def test_catalog_selected_follows_setting(self, client):
        # The briefing_tts_model setting wins when it names a catalog entry;
        # an unknown stored value falls back to the default.
        r = await client.put("/api/settings", json={"briefing_tts_model": "voxtral-4b"})
        assert r.status_code == 200
        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=0),
        ):
            resp = await client.get("/api/tts/catalog")
        assert resp.json()["selected"] == "voxtral-4b"

    async def test_catalog_lists_narrator_voices(self, client):
        from memory_core.tts_local import VALID_VOICES

        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=0),
        ):
            resp = await client.get("/api/tts/catalog")
        voices = resp.json()["voices"]
        # Sorted, complete, and drift-proof against the engine's own list.
        assert voices == sorted(VALID_VOICES)

    async def test_catalog_reports_downloaded_size(self, client):
        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=True),
            patch("memory_core.tts_local.model_size_bytes", return_value=2_400_000_000),
        ):
            resp = await client.get("/api/tts/catalog")
        vox = next(m for m in resp.json()["models"] if m["key"] == "voxtral-4b")
        assert vox["downloaded"] is True
        assert vox["size_bytes"] == 2_400_000_000


class TestTtsDelete:
    async def test_delete_existing(self, client):
        with patch("memory_core.tts_local.delete_model", return_value=True) as rm:
            resp = await client.post("/api/tts/delete", json={"key": "voxtral-4b"})
        assert resp.status_code == 200
        assert resp.json() == {"key": "voxtral-4b", "deleted": True}
        rm.assert_called_once()

    async def test_delete_absent_is_noop(self, client):
        with patch("memory_core.tts_local.delete_model", return_value=False):
            resp = await client.post("/api/tts/delete", json={"key": "voxtral-4b"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False

    async def test_delete_unknown_key_rejected(self, client):
        resp = await client.post("/api/tts/delete", json={"key": "bogus"})
        assert resp.status_code == 400

    async def test_delete_oserror_returns_500_with_opaque_message(self, client):
        """A filesystem failure while deleting the snapshot maps to 500.

        The route catches ``OSError`` from ``delete_model`` and returns an
        opaque code — the raw exception (which can carry a real on-disk path)
        is logged server-side, never leaked to the browser.
        """
        with patch(
            "memory_core.tts_local.delete_model",
            side_effect=OSError("[Errno 13] Permission denied: '/snap/voxtral'"),
        ) as rm:
            resp = await client.post("/api/tts/delete", json={"key": "voxtral-4b"})
        assert resp.status_code == 500
        body = resp.json()
        assert body == {"error": "delete_failed (see server logs)"}
        # The leak guard: the on-disk path in the exception must not surface.
        assert "/snap/voxtral" not in resp.text
        rm.assert_called_once()


class TestTtsDownload:
    async def test_download_already_downloaded_short_circuits(self, client):
        dl = AsyncMock()
        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=True),
            patch("memory_core.tts_local.download_model", dl),
        ):
            resp = await client.get("/api/tts/download?key=voxtral-4b")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[0] == {"message": "Starting download…"}
        assert events[-1]["status"] == "done"
        assert "Already downloaded" in events[-1]["message"]
        # Short-circuit: the (synchronous) download was never kicked off.
        dl.assert_not_awaited()

    async def test_download_success_ends_with_done(self, client):
        """A missing snapshot triggers download_model and ends with 'done'."""

        def fake_download(*_args, **_kwargs):
            return "/snap/voxtral"

        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=0),
            patch("memory_core.tts_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.tts.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/tts/download?key=voxtral-4b")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[0] == {"message": "Starting download…"}
        assert events[-1] == {"status": "done", "message": "✓ Ready"}

    async def test_download_reports_progress_percentage(self, client):
        """While the snapshot grows, a progress event with a clamped pct fires.

        ``model_size_bytes`` reports a partial size mid-download; the route
        divides it by the catalog's ``expected_bytes`` and clamps to 99 so the
        bar never shows 100% before completion.
        """
        from memory_core.tts_local import TTS_CATALOG

        expected = TTS_CATALOG["voxtral-4b"]["expected_bytes"]
        # Half-written snapshot → ~50% (well under the 99 clamp).
        half = expected // 2

        def fake_download(*_args, **_kwargs):
            return "/snap/voxtral"

        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=half),
            patch("memory_core.tts_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.tts.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/tts/download?key=voxtral-4b")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        progress = [e for e in events if "progress" in e]
        assert progress, "expected at least one progress event mid-download"
        assert progress[0]["progress"] == 50
        assert "Downloading… 50%" in progress[0]["message"]
        assert events[-1]["status"] == "done"

    async def test_download_progress_is_clamped_to_99(self, client):
        """An over-full size reading still reports at most 99% pre-completion."""
        from memory_core.tts_local import TTS_CATALOG

        expected = TTS_CATALOG["voxtral-4b"]["expected_bytes"]

        def fake_download(*_args, **_kwargs):
            return "/snap/voxtral"

        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            # Report more bytes than expected — pct math would exceed 100.
            patch("memory_core.tts_local.model_size_bytes", return_value=expected * 2),
            patch("memory_core.tts_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.tts.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/tts/download?key=voxtral-4b")
        events = _sse_events(resp.text)
        progress = [e for e in events if "progress" in e]
        assert progress and progress[0]["progress"] == 99
        assert events[-1]["status"] == "done"

    async def test_download_failure_surfaces_error_event_not_500(self, client):
        """A failing download yields an 'error' SSE event, never an HTTP 500."""

        def boom(*_args, **_kwargs):
            raise RuntimeError("HF CDN unreachable: token /home/me/.hf/token")

        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=0),
            patch("memory_core.tts_local.download_model", side_effect=boom),
            patch("estormi_server.api.tts.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/tts/download?key=voxtral-4b")
        # The stream itself succeeds; the failure is in-band.
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[-1] == {
            "status": "error",
            "message": "Error: download_failed (see server logs)",
        }
        # The raw exception (which carried a token path) must not leak.
        assert "/home/me/.hf/token" not in resp.text

    async def test_download_unknown_key_falls_back_to_default_expected(self, client):
        """An unrecognised ?key= still streams — expected_bytes falls back.

        The route resolves ``expected_bytes`` via ``TTS_CATALOG.get(key, {})``
        with a default, so an unknown key cannot crash the progress math.
        """

        def fake_download(*_args, **_kwargs):
            return "/snap/voxtral"

        with (
            patch("memory_core.tts_local.is_model_downloaded", return_value=False),
            patch("memory_core.tts_local.model_size_bytes", return_value=0),
            patch("memory_core.tts_local.download_model", side_effect=fake_download),
            patch("estormi_server.api.tts.asyncio.sleep", _yield_sleep),
        ):
            resp = await client.get("/api/tts/download?key=bogus-voice")
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert events[-1]["status"] == "done"
