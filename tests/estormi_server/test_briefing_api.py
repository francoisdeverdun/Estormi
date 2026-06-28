"""/api/knowledge/* — REST endpoints for status, run, log, stop."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration

# ── API endpoints ─────────────────────────────────────────────────────────────


async def test_api_knowledge_status_fields(client):
    r = await client.get("/api/knowledge/status")
    assert r.status_code == 200
    data = r.json()
    assert "running" in data
    assert "knowledge_last_run_status" in data
    assert "knowledge_last_run_at" in data
    assert "knowledge_last_run_summary" in data


async def test_api_knowledge_status_reflects_db(client, db):
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('knowledge_last_run_status', 'ok')"
    )
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('knowledge_last_run_summary', '6 sources, 4 new items')"
    )
    await db.commit()

    with patch(
        "estormi_server.server.jobs._briefing_running", new_callable=AsyncMock, return_value=False
    ):
        r = await client.get("/api/knowledge/status")

    assert r.json()["knowledge_last_run_status"] == "ok"
    assert "6 sources" in r.json()["knowledge_last_run_summary"]


async def test_api_knowledge_run_enqueues_briefing(client):
    """``/api/knowledge/run`` enqueues a briefing run on the engine room
    queue (the runner picks it up when the engine slot frees)."""
    from estormi_server.server import jobs

    jobs._queue.clear()
    jobs._running = None
    r = await client.post("/api/knowledge/run")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert [e.kind for e in jobs._queue] == ["briefing"]
    assert jobs._queue[0].source == "manual"
    jobs._queue.clear()


async def test_api_knowledge_run_reports_already_running(client):
    """A click while briefing is running returns ``already_running`` so
    the UI can show a friendly status instead of a 409 spike."""
    from estormi_server.server import jobs

    jobs._queue.clear()
    jobs._running = "briefing"
    try:
        r = await client.post("/api/knowledge/run")
        assert r.status_code == 200
        assert r.json()["status"] == "already_running"
        assert jobs._queue == []
    finally:
        jobs._running = None


async def test_api_knowledge_log_not_found(client, tmp_path):
    with patch("estormi_server.server.jobs._KNOWLEDGE_LOG", tmp_path / "nonexistent.log"):
        r = await client.get("/api/knowledge/log")
    assert r.status_code == 200
    assert "not found" in r.json()["content"]


async def test_api_knowledge_log_returns_tail(client, tmp_path):
    log_file = tmp_path / "knowledge.log"
    log_file.write_text("line1\nline2\nline3\n")
    with patch("estormi_server.server.jobs._KNOWLEDGE_LOG", log_file):
        r = await client.get("/api/knowledge/log")
    assert r.status_code == 200
    assert "line1" in r.json()["content"]
    assert "line3" in r.json()["content"]


async def test_api_knowledge_stop_when_not_running(client):
    with patch(
        "estormi_server.server.jobs._briefing_running", new_callable=AsyncMock, return_value=False
    ):
        r = await client.post("/api/knowledge/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "not_running"


async def test_api_knowledge_stop_kills_briefing(client, db):
    """Stop endpoint kills the briefing engine and marks status stopped."""
    with (
        patch(
            "estormi_server.server.jobs._briefing_running",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("estormi_server.server.jobs._kill_briefing", new_callable=AsyncMock) as mock_kill,
    ):
        r = await client.post("/api/knowledge/stop")

    assert r.status_code == 200
    assert r.json()["status"] == "stopped"
    mock_kill.assert_awaited_once()

    cur = await db.execute("SELECT value FROM settings WHERE key = 'knowledge_last_run_status'")
    row = await cur.fetchone()
    assert row and row[0] == "stopped"


# ── log read failure ──────────────────────────────────────────────────────────


async def test_api_knowledge_log_read_error_returns_500(client, tmp_path):
    """A read failure on an existing log surfaces a 500, not a stack trace."""
    log_file = tmp_path / "knowledge.log"
    log_file.write_text("line\n")
    with (
        patch("estormi_server.server.jobs._KNOWLEDGE_LOG", log_file),
        patch("estormi_server.server.log_tail.tail_lines", side_effect=OSError("disk gone")),
    ):
        r = await client.get("/api/knowledge/log")
    assert r.status_code == 500
    assert r.json()["error"] == "knowledge log read failed"


# ── briefings: list / get / delete ────────────────────────────────────────────


async def test_api_list_briefings_returns_vault_items(client):
    items = [{"date": "2026-06-06", "title": "Yesterday"}]
    with patch("estormi_ingestion.shared.delivery.vault_sync.list_briefings", return_value=items):
        r = await client.get("/api/briefings")
    assert r.status_code == 200
    assert r.json() == {"items": items}


async def test_api_get_briefing_rejects_bad_date(client):
    r = await client.get("/api/briefings/not-a-date")
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]


async def test_api_get_briefing_404_when_absent(client):
    with patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=None):
        r = await client.get("/api/briefings/2026-06-06")
    assert r.status_code == 404


async def test_api_get_briefing_returns_payload(client):
    payload = {"date": "2026-06-06", "htmlBody": "<p>hi</p>"}
    with patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=payload):
        r = await client.get("/api/briefings/2026-06-06")
    assert r.status_code == 200
    assert r.json() == payload


async def test_api_edit_briefing_saves_and_folds_into_distill(client):
    """A macOS edit saves the corrected briefing to the vault, folds the
    correction into the quill's training set, and nudges the iOS companion with
    an "updated" APNs banner (not the "new briefing" doorbell)."""
    existing = {"date": "2026-06-06", "htmlBody": "<p>old</p>", "title": "B"}
    with (
        patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=existing),
        patch(
            "estormi_ingestion.shared.delivery.vault_sync.push_briefing", return_value=True
        ) as push,
        patch(
            "estormi_ingestion.shared.delivery.vault_sync.notify_briefing_updated"
        ) as notify_updated,
        patch("estormi_server.api.knowledge._fold_edit_into_distill") as fold,
    ):
        r = await client.put("/api/briefings/2026-06-06", json={"htmlBody": "<p>corrected</p>"})
    assert r.status_code == 200
    assert r.json() == {"date": "2026-06-06", "saved": True}
    saved_briefing, notify = push.call_args.args
    assert saved_briefing["htmlBody"] == "<p>corrected</p>"
    assert "editedAt" in saved_briefing
    assert notify is False  # push_briefing does NOT fire the new-briefing doorbell
    fold.assert_called_once_with("2026-06-06", "<p>corrected</p>")
    notify_updated.assert_called_once_with("2026-06-06")  # iOS "updated" nudge


async def test_api_edit_briefing_structured_fields_splice(client):
    """A structured (per-section) edit re-renders only the named section and
    splices it into the stored body, leaving the other sections + their markers
    intact. The training fold-in sees the new, spliced body."""
    from estormi_briefing.compose.build_daily_note import briefing_fields, build_note

    vision = (
        "READINESS: solid recovery this morning.\n\n"
        "OBJECTIVE: A focused build day.\n\n"
        "The first real paragraph carries the plan."
    )
    html = build_note("2026-06-06", 1, 0, vision_html=vision, lang="en")
    existing = {
        "date": "2026-06-06",
        "htmlBody": html,
        "title": "B",
        "lang": "en",
        "fields": briefing_fields(vision),
    }
    with (
        patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=existing),
        patch(
            "estormi_ingestion.shared.delivery.vault_sync.push_briefing", return_value=True
        ) as push,
        patch("estormi_server.api.knowledge._fold_edit_into_distill") as fold,
    ):
        r = await client.put(
            "/api/briefings/2026-06-06",
            json={"fields": {"objective": "A corrected through-line"}},
        )
    assert r.status_code == 200
    saved, notify = push.call_args.args
    body = saved["htmlBody"]
    assert "A corrected through-line" in body  # objective re-rendered
    assert "A focused build day" not in body  # old objective replaced
    assert "solid recovery this morning" in body  # readiness untouched
    assert "The first real paragraph carries the plan" in body  # my-day untouched
    assert saved["fields"]["objective"] == "A corrected through-line"
    assert saved["fields"]["readiness"] == "solid recovery this morning."  # preserved
    assert notify is False
    assert fold.call_args.args[0] == "2026-06-06"
    assert "A corrected through-line" in fold.call_args.args[1]  # trains on spliced body


async def test_api_edit_briefing_fields_422_without_markers(client):
    """Field edits on a briefing that predates the zone markers are rejected, so
    the SPA falls back to the raw-HTML editor instead of silently no-op'ing."""
    existing = {"date": "2026-06-06", "htmlBody": "<p>no markers</p>", "title": "B", "lang": "en"}
    with patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=existing):
        r = await client.put("/api/briefings/2026-06-06", json={"fields": {"objective": "x"}})
    assert r.status_code == 422


async def test_api_edit_briefing_requires_html_or_fields(client):
    r = await client.put("/api/briefings/2026-06-06", json={})
    assert r.status_code == 400


async def test_api_edit_briefing_404_when_absent(client):
    with patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=None):
        r = await client.put("/api/briefings/2026-06-06", json={"htmlBody": "x"})
    assert r.status_code == 404


async def test_api_edit_briefing_rejects_bad_date(client):
    r = await client.put("/api/briefings/nope", json={"htmlBody": "x"})
    assert r.status_code == 400


async def test_api_delete_briefing_rejects_bad_date(client):
    r = await client.delete("/api/briefings/06-06-2026")
    assert r.status_code == 400


async def test_api_delete_briefing_removes_chunks_and_vault(client):
    with (
        patch(
            "estormi_server.api.knowledge.delete_by_source_id",
            new_callable=AsyncMock,
            return_value={"deleted": 2},
        ),
        patch(
            "estormi_ingestion.shared.delivery.vault_sync.delete_briefing", return_value=True
        ) as vault_del,
    ):
        r = await client.delete("/api/briefings/2026-06-06")
    assert r.status_code == 200
    assert r.json() == {"deleted": 2, "date": "2026-06-06", "vault": True}
    vault_del.assert_called_once_with("2026-06-06")


# ── briefings reset ───────────────────────────────────────────────────────────


async def test_api_briefings_reset_wipes_history(client, db, tmp_path):
    """Reset clears briefing_runs + last-run settings and reports the counts."""
    await db.execute(
        "INSERT INTO briefing_runs (started_at, status) VALUES ('2026-06-06T10:00:00+00:00', 'ok')"
    )
    await db.execute("INSERT INTO settings (key, value) VALUES ('knowledge_last_run_status', 'ok')")
    await db.commit()

    with (
        patch("estormi_server.server.jobs.remove_from_queue", new_callable=AsyncMock),
        patch(
            "estormi_server.server.jobs._briefing_running",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("estormi_server.server.jobs._KNOWLEDGE_LOG", tmp_path / "nonexistent.log"),
        patch(
            "estormi_server.storage.writers.delete_by_source",
            new_callable=AsyncMock,
            return_value={"deleted": 0},
        ),
        patch("estormi_ingestion.shared.delivery.vault_sync.vault_dir", return_value=tmp_path),
        patch("estormi_ingestion.shared.delivery.vault_sync._rebuild_manifest"),
        patch("memory_core.audit.log_security_decision"),
    ):
        r = await client.post("/api/briefings/reset")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["chunks_deleted"] == 0
    assert body["vault_files_deleted"] == 0

    # Observable effect: the run history and last-run setting are gone.
    cur = await db.execute("SELECT COUNT(*) FROM briefing_runs")
    assert (await cur.fetchone())[0] == 0
    cur = await db.execute("SELECT COUNT(*) FROM settings WHERE key = 'knowledge_last_run_status'")
    assert (await cur.fetchone())[0] == 0


# ── runs history + JSON column parsing ────────────────────────────────────────


def test_parse_json_col_decodes_and_falls_back():
    from estormi_server.api.knowledge import _parse_json_col

    assert _parse_json_col(None) == {}
    assert _parse_json_col("") == {}
    assert _parse_json_col('{"HEALTH": 3}') == {"HEALTH": 3}
    assert _parse_json_col("[1, 2]") == [1, 2]
    # A corrupt blob must not bubble up — fall back to {}.
    assert _parse_json_col("{not json") == {}


async def test_api_knowledge_runs_returns_parsed_rows(client, db):
    from datetime import datetime, timezone

    recent = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO briefing_runs "
        "(started_at, status, duration_ms, model, tokens_in, tokens_out, "
        " sections_json, items_considered, items_included, summary) "
        "VALUES (?, 'ok', 1200, 'local', 10, 20, '{\"HEALTH\": 3}', 8, 5, 'done')",
        (recent,),
    )
    # A NULL sections_json must decode to {} via _parse_json_col, not crash.
    await db.execute(
        "INSERT INTO briefing_runs (started_at, status, sections_json) VALUES (?, 'ok', NULL)",
        (recent,),
    )
    await db.commit()

    r = await client.get("/api/knowledge/runs?days=30&limit=50")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 2
    first = next(x for x in runs if x["model"] == "local")
    assert first["sections"] == {"HEALTH": 3}
    assert first["duration_ms"] == 1200
    assert all(x["sections"] == {} or isinstance(x["sections"], dict) for x in runs)
