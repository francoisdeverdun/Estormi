"""Tests for the wake-time health refresh (readiness-only briefing update)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import estormi_briefing.refresh_health as refresh_health
from estormi_briefing.compose.build_daily_note import _readiness_card

pytestmark = pytest.mark.integration


def _briefing_with_card() -> dict:
    card = _readiness_card("Forme provisoire, en attente de ta nuit.", "fr")
    return {
        "id": "briefing-2026-06-11",
        "date": "2026-06-11",
        "title": "Briefing du 11 juin 2026",
        "htmlBody": f"<h1>Briefing</h1>{card}<p>Ma journée…</p>",
        "generatedAt": "2026-06-11T05:24:00Z",
    }


def _health_chunk() -> dict:
    return {
        "source": "whoop",
        "date_ts": "2026-06-11T07:10:00+00:00",
        "title": "WHOOP recovery",
        "text": "Recovery 82%, sommeil 8h02, HRV 78 ms",
    }


async def test_refresh_splices_pushes_then_silently_updates_audio(actions_db, monkeypatch):
    pushes: list[tuple[str, bool]] = []

    def fake_push(briefing, notify=True):
        pushes.append((briefing["htmlBody"], notify))
        return True

    async def fake_audio(db, today, body, briefing, provider, model):
        briefing["audioPath"] = f"briefings/{today}.m4a"

    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=_briefing_with_card()),
        patch.object(refresh_health, "push_briefing", side_effect=fake_push),
        patch.object(
            refresh_health, "_fetch_health_chunks", AsyncMock(return_value=[_health_chunk()])
        ),
        patch.object(
            refresh_health,
            "_write_readiness",
            AsyncMock(return_value="READINESS: Récupération à 82 % : tu peux pousser."),
        ),
        patch.object(refresh_health, "_maybe_attach_audio", side_effect=fake_audio),
        patch("estormi_briefing.run_briefing.DB_PATH", ":memory:"),
        patch.object(refresh_health.aiosqlite, "connect", AsyncMock(return_value=actions_db)),
        patch.object(actions_db, "close", AsyncMock()),
    ):
        summary = await refresh_health.run_refresh()

    assert "readiness updated" in summary
    assert len(pushes) == 2
    text_body, text_notify = pushes[0]
    audio_body, audio_notify = pushes[1]
    assert "82" in text_body and "provisoire" not in text_body
    assert text_notify is True  # the wake-time text update notifies
    assert audio_notify is False  # the audio follow-up is silent
    assert "Ma journée…" in text_body  # rest of the briefing untouched


async def test_refresh_preserves_morning_summary(actions_db, monkeypatch):
    """A wake-time readiness refresh must not clobber the morning full run's
    rich knowledge_last_run_summary — it appends its note instead."""
    await actions_db.execute(
        "INSERT OR REPLACE INTO settings (key, value) "
        "VALUES ('knowledge_last_run_summary', '6 sources, 4 new items, 3 actions')"
    )
    await actions_db.commit()

    async def fake_audio(db, today, body, briefing, provider, model):
        return None

    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=_briefing_with_card()),
        patch.object(refresh_health, "push_briefing", MagicMock(return_value=True)),
        patch.object(
            refresh_health, "_fetch_health_chunks", AsyncMock(return_value=[_health_chunk()])
        ),
        patch.object(
            refresh_health,
            "_write_readiness",
            AsyncMock(return_value="READINESS: Récupération à 82 % : tu peux pousser."),
        ),
        patch.object(refresh_health, "_maybe_attach_audio", side_effect=fake_audio),
        patch("estormi_briefing.run_briefing.DB_PATH", ":memory:"),
        patch.object(refresh_health.aiosqlite, "connect", AsyncMock(return_value=actions_db)),
        patch.object(actions_db, "close", AsyncMock()),
    ):
        await refresh_health.run_refresh()

    cur = await actions_db.execute(
        "SELECT value FROM settings WHERE key = 'knowledge_last_run_summary'"
    )
    row = await cur.fetchone()
    await cur.close()
    persisted = row[0]
    assert "6 sources, 4 new items, 3 actions" in persisted  # morning summary survives
    assert "readiness updated" in persisted  # refresh note appended


async def test_refresh_keeps_morning_summary_when_no_health_data(actions_db):
    """With no health data the briefing is left untouched, so the morning
    summary must be preserved verbatim (only the status is refreshed)."""
    await actions_db.execute(
        "INSERT OR REPLACE INTO settings (key, value) "
        "VALUES ('knowledge_last_run_summary', '6 sources, 4 new items, 3 actions')"
    )
    await actions_db.commit()

    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=_briefing_with_card()),
        patch.object(refresh_health, "push_briefing", MagicMock()),
        patch.object(refresh_health, "_fetch_health_chunks", AsyncMock(return_value=[])),
        patch("estormi_briefing.run_briefing.DB_PATH", ":memory:"),
        patch.object(refresh_health.aiosqlite, "connect", AsyncMock(return_value=actions_db)),
        patch.object(actions_db, "close", AsyncMock()),
    ):
        await refresh_health.run_refresh()

    cur = await actions_db.execute(
        "SELECT value FROM settings WHERE key = 'knowledge_last_run_summary'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == "6 sources, 4 new items, 3 actions"  # untouched


async def test_refresh_falls_back_to_full_run_when_no_briefing(monkeypatch):
    full_run = AsyncMock(return_value="9 sources, …")
    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=None),
        patch("estormi_briefing.run_briefing.run", full_run),
    ):
        out = await refresh_health.run_refresh()
    full_run.assert_awaited_once()
    assert "sources" in out


async def test_refresh_announces_existing_briefing_without_health_data(actions_db):
    """No fresh health data → the readiness can't be recomposed, but the morning
    briefing (composed silently) must still be ANNOUNCED at wake, not dropped."""
    pushes: list[bool] = []

    def fake_push(briefing, notify=True):
        pushes.append(notify)
        return True

    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=_briefing_with_card()),
        patch.object(refresh_health, "push_briefing", side_effect=fake_push),
        patch.object(refresh_health, "_fetch_health_chunks", AsyncMock(return_value=[])),
        patch("estormi_briefing.run_briefing.DB_PATH", ":memory:"),
        patch.object(refresh_health.aiosqlite, "connect", AsyncMock(return_value=actions_db)),
        patch.object(actions_db, "close", AsyncMock()),
    ):
        summary = await refresh_health.run_refresh()
    assert pushes == [True]  # announced exactly once, with notify on
    assert "announced" in summary


async def test_refresh_announces_existing_briefing_when_readiness_fails(actions_db):
    """A failed/empty readiness recompose must still ANNOUNCE the already-composed
    morning briefing — a flaky LLM call once swallowed the entire wake push."""
    pushes: list[tuple[str, bool]] = []

    def fake_push(briefing, notify=True):
        pushes.append((briefing["htmlBody"], notify))
        return True

    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=_briefing_with_card()),
        patch.object(refresh_health, "push_briefing", side_effect=fake_push),
        patch.object(
            refresh_health, "_fetch_health_chunks", AsyncMock(return_value=[_health_chunk()])
        ),
        # The readiness recompose yields nothing usable (the LLM call failed).
        patch.object(refresh_health, "_write_readiness", AsyncMock(return_value="")),
        patch("estormi_briefing.run_briefing.DB_PATH", ":memory:"),
        patch.object(refresh_health.aiosqlite, "connect", AsyncMock(return_value=actions_db)),
        patch.object(actions_db, "close", AsyncMock()),
    ):
        summary = await refresh_health.run_refresh()

    assert len(pushes) == 1  # the existing briefing is announced once
    body, notify = pushes[0]
    assert notify is True
    assert "provisoire" in body  # the morning readiness card is untouched, not recomposed
    assert "announced" in summary
    cur = await actions_db.execute(
        "SELECT value FROM settings WHERE key = 'knowledge_last_run_status'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == "ok"  # the announce succeeded; a skipped readiness is not a hard failure


async def test_refresh_updates_fields_readiness_in_lockstep(actions_db):
    """The wake-time refresh must update fields['readiness'] alongside the
    htmlBody card — otherwise the structured field goes stale (the 2026-06-20
    and -22 htmlBody≠fields divergence)."""
    captured: list[dict] = []

    def fake_push(briefing, notify=True):
        captured.append({**briefing, "fields": dict(briefing.get("fields") or {})})
        return True

    async def fake_audio(db, today, body, briefing, provider, model):
        return None

    with (
        patch.object(refresh_health, "_ingest_whoop", AsyncMock()),
        patch.object(refresh_health, "read_briefing", return_value=_briefing_with_card()),
        patch.object(refresh_health, "push_briefing", side_effect=fake_push),
        patch.object(
            refresh_health, "_fetch_health_chunks", AsyncMock(return_value=[_health_chunk()])
        ),
        patch.object(
            refresh_health,
            "_write_readiness",
            AsyncMock(return_value="READINESS: Récupération à 82 % : tu peux pousser."),
        ),
        patch.object(refresh_health, "_maybe_attach_audio", side_effect=fake_audio),
        patch("estormi_briefing.run_briefing.DB_PATH", ":memory:"),
        patch.object(refresh_health.aiosqlite, "connect", AsyncMock(return_value=actions_db)),
        patch.object(actions_db, "close", AsyncMock()),
    ):
        await refresh_health.run_refresh()

    assert captured
    assert captured[0]["fields"]["readiness"] == "Récupération à 82 % : tu peux pousser."
