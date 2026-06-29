"""Distillation endpoints — status shape and run enqueue."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestDistillStatus:
    async def test_status_shape(self, client):
        resp = await client.get("/api/distill/status")
        assert resp.status_code == 200
        data = resp.json()
        assert {"status", "references", "tooling", "installed", "running"} <= data.keys()
        assert data["references"]["count"] == 0
        assert "vaultCount" in data["references"]  # trainable count, even pre-harvest
        assert data["tooling"]["ready"] is False  # no MLX tooling in the test env
        assert data["installed"] is False

    async def test_running_includes_the_active_engine(self, client, monkeypatch):
        # The card's "active" flag reads `running`. A solo run has an empty
        # queue, so the endpoint must fold in the engine the event bus reports
        # as executing — otherwise the live progress block stays hidden.
        from estormi_server.server import events as engine_events

        monkeypatch.setattr(engine_events, "current_kind", lambda: "distill")
        resp = await client.get("/api/distill/status")
        assert resp.status_code == 200
        assert any(e["kind"] == "distill" for e in resp.json()["running"])


class TestDistillRun:
    async def test_run_enqueues_without_tooling(self, client, monkeypatch):
        # No tooling gate: the chain self-bootstraps its MLX toolchain (engine
        # phase ⓪), so the run enqueues even on a machine that never set it up.
        async def fake_enqueue(kind, source="manual", payload=None):
            assert kind == "distill"
            return "queued"

        from estormi_server.server import jobs

        monkeypatch.setattr(jobs, "enqueue", fake_enqueue)
        resp = await client.post("/api/distill/run")
        assert resp.status_code == 200
        assert resp.json() == {"status": "queued"}


class TestDistillLog:
    async def test_log_returns_content_key(self, client):
        # Distill has its own log file (the engine-room view must NOT fall back
        # to the briefing log). Absent a run, the endpoint still returns a
        # ``content`` string rather than erroring.
        resp = await client.get("/api/distill/log?lines=50")
        assert resp.status_code == 200
        assert "content" in resp.json()


class TestDistillTooling:
    async def test_delete_returns_removed_flag(self, client):
        # No toolchain in the test env, so removal reports the dir as gone.
        resp = await client.post("/api/distill/tooling/delete")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
