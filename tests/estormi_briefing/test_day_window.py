"""Day-vision context now comes from a fetch_around time-window bundle (Phase 4)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import estormi_briefing.day.day_context as day_context
import estormi_briefing.day.day_vision as day_vision
from estormi_briefing.day.day import _format_action

pytestmark = pytest.mark.unit


def test_parse_event_location_gcal_format():
    """gcal chunks put the location on the line after the ``→`` time line."""
    text = "Standup\n2026-05-19T10:00 → 2026-05-19T10:30\nRoom 4, HQ\nweekly sync"
    assert day_vision._parse_event_location(text) == "Room 4, HQ"


def test_parse_event_location_apple_single_line_format():
    """Apple Calendar chunks are whitespace-collapsed to one line and tag the
    location with a ``Location:`` label — the gcal ``→`` heuristic misses it."""
    text = (
        "Calendar: Perso Title: Déjeuner Start: 2026-05-19T12:00 "
        "End: 2026-05-19T13:00 Location: Chez Paul, Paris"
    )
    assert day_vision._parse_event_location(text) == "Chez Paul, Paris"


def test_parse_event_location_absent():
    assert day_vision._parse_event_location("Standup\nno time line here") == ""
    assert day_vision._parse_event_location("") == ""


def test_format_action_reads_event_type_and_tentative():
    """A gcal row's structured columns surface as `event_type` + `tentative`
    on the action dict — no chunk-text round-trip."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 'gcal' AS source, 'Day off' AS title, NULL AS date, "
        "'2026-02-01T10:00:00+00:00' AS date_ts, 'work' AS group_type, "
        "NULL AS chat_id_raw, 'outOfOffice' AS event_type, 'tentative' AS event_status"
    ).fetchone()
    action = _format_action(row)
    assert action["event_type"] == "outOfOffice"
    assert action["tentative"] is True


def test_format_action_defaults_for_reminder_row():
    """Reminder rows omit the calendar-only columns; _format_action reads them
    defensively and falls back to a confirmed, default-type slot."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 'reminders' AS source, 'Call bank' AS title, NULL AS date, "
        "'2026-02-01T10:00:00+00:00' AS date_ts, 'me' AS group_type, NULL AS chat_id_raw"
    ).fetchone()
    action = _format_action(row)
    assert action["event_type"] == "default"
    assert action["tentative"] is False


async def test_today_located_events_surfaces_work_location():
    """One MCP fetch yields located events (location parsed from the chunk text)
    plus the day's working location — now a structured chunk field, not a text
    trailer. The work location survives even on an untimed entry that never
    enters the located list used for travel."""

    async def _fake(payload, timeout=12.0):
        return [
            {
                "source": "gcal",
                "title": "Sprint review",
                "group_type": "work",
                "date_ts": "2026-02-01T10:00:00+00:00",
                "end_date_ts": "2026-02-01T11:00:00+00:00",
                "working_location": "FR-DigitalFactory (office)",
                "text": "Sprint review\n2026-02-01T10:00 → 2026-02-01T11:00\nParis HQ",
            },
            # An untimed entry (no date_ts) still contributes the work location.
            {
                "source": "gcal",
                "title": "FR-DigitalFactory",
                "group_type": "work",
                "date_ts": None,
                "working_location": "FR-DigitalFactory (office)",
                "text": "FR-DigitalFactory",
            },
        ]

    with patch.object(day_vision, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        events, work_location = await day_vision._fetch_today_located_events(date(2026, 2, 1))

    assert work_location == "FR-DigitalFactory (office)"
    assert [e["title"] for e in events] == ["Sprint review"]
    assert events[0]["location"] == "Paris HQ"


async def test_day_context_uses_fetch_around_personal_window():
    """`_fetch_day_context_chunks` calls /fetch_around scoped to the personal
    corpus and a window centred on the briefing day — not a keyword search."""
    captured: dict = {}

    async def _fake(payload, timeout=12.0):
        captured.update(payload)
        return [
            {"source": "mail", "title": "Devis", "text": "le devis", "group_type": ""},
            {"source": "reminders", "title": "Acheter vin", "text": "vin", "group_type": ""},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_day_context_chunks(date(2026, 5, 20), limit=12)

    assert captured["date"] == "2026-05-20"
    assert captured["corpus"] == "personal"
    assert captured["window_days"] == day_context._BRIEFING_WINDOW_DAYS
    assert captured["forward_days"] == 0  # look-back only — no next-day leak
    assert {c["source"] for c in out} == {"mail", "reminders"}


async def test_fetch_recent_whatsapp_filters_by_recency_and_group():
    """`_fetch_recent_whatsapp` keeps only recent chunks in actionable group
    types — replacing the old pending-reply flag with a plain recency window the
    day-vision then judges. Runs against *today's* briefing day: the recency
    cutoff anchors on the end of the briefing day, so now-relative chunks only
    make sense for a same-day run."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=3)).isoformat()
    stale = (now - timedelta(hours=80)).isoformat()
    captured: dict = {}

    async def _fake(payload, timeout=12.0):
        captured.update(payload)
        return [
            {
                "source": "whatsapp",
                "text": "fresh friend ping",
                "group_type": "friends",
                "date_ts": recent,
            },
            {"source": "whatsapp", "text": "old thread", "group_type": "friends", "date_ts": stale},
            {
                "source": "whatsapp",
                "text": "charity noise",
                "group_type": "charity",
                "date_ts": recent,
            },
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_recent_whatsapp(datetime.now().astimezone().date(), hours=48)

    assert captured["sources"] == ["whatsapp"]
    assert captured["corpus"] == "personal"
    # Only the recent, actionable-group chunk survives; stale + charity dropped.
    assert [c["text"] for c in out] == ["fresh friend ping"]


async def test_fetch_recent_whatsapp_caps_and_sorts_newest_first():
    """Output is newest-first and capped at _WA_RECENT_MAX_CHUNKS."""
    now = datetime.now(timezone.utc)
    cap = day_context._WA_RECENT_MAX_CHUNKS

    async def _fake(payload, timeout=12.0):
        # cap + 5 chunks, each a minute apart, all recent and in an actionable group
        return [
            {
                "source": "whatsapp",
                "text": f"msg {i}",
                "group_type": "friends",
                "date_ts": (now - timedelta(minutes=i)).isoformat(),
            }
            for i in range(cap + 5)
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        # now-relative chunks → run against today so they fall inside the
        # [cutoff, day-end] recency window. The upper bound (day-end) rejects
        # future-dated chats on a rebuilt past day, so a past briefing date here
        # would correctly drop them all.
        out = await day_context._fetch_recent_whatsapp(datetime.now().astimezone().date())

    assert len(out) == cap
    assert out[0]["text"] == "msg 0"  # newest first


async def test_day_context_drops_generated_briefing_notes():
    """A prior day's briefing lands in the personal window; it must not feed
    the next briefing's context (that would compound stale regurgitation)."""

    async def _fake(payload, timeout=12.0):
        return [
            {
                "source": "briefing",
                "title": "Briefing — 2026-05-19",
                "text": "old",
                "group_type": "",
            },
            {"source": "calendar", "title": "Dîner", "text": "20h", "group_type": "me"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_day_context_chunks(date(2026, 5, 20))

    assert all(c["source"] != "briefing" for c in out)


async def test_day_context_admits_org_calendar_holidays():
    """Holiday / org-calendar dates (group_type='organisation') reach the
    day-vision context so it can correlate them with personal events."""

    async def _fake(payload, timeout=12.0):
        return [
            {
                "source": "gcal",
                "title": "Fête des Mères",
                "text": "Holiday",
                "group_type": "organisation",
            },
            {"source": "gcal", "title": "Standup", "text": "9h", "group_type": "work"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_day_context_chunks(date(2026, 5, 31))

    assert "Fête des Mères" in {c["title"] for c in out}


async def test_day_context_keeps_holiday_anchor_despite_cap():
    """A lone holiday anchor is retained even when voluminous chat would
    otherwise crowd it out of the capped window — it's pulled to the front."""

    async def _fake(payload, timeout=12.0):
        chats = [
            {"source": "whatsapp", "title": f"c{i}", "text": "hi", "group_type": "family"}
            for i in range(20)
        ]
        holiday = {
            "source": "gcal",
            "title": "Fête des Mères",
            "text": "Holiday",
            "group_type": "organisation",
        }
        return chats + [holiday]  # holiday last → a naive [:12] would drop it

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_day_context_chunks(date(2026, 5, 31), limit=12)

    assert len(out) == 12
    assert "Fête des Mères" in {c["title"] for c in out}


async def test_day_context_admits_group_and_sport_whatsapp():
    """Generic group / sport chats reach the correlation context (the
    schedule-grade set omits them, but crew/club coordination lives there)."""

    async def _fake(payload, timeout=12.0):
        return [
            {"source": "whatsapp", "title": "t", "text": "ça cours", "group_type": "group"},
            {"source": "whatsapp", "title": "t", "text": "marathon", "group_type": "sport"},
            {"source": "whatsapp", "title": "t", "text": "spam", "group_type": "broadcast"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_day_context_chunks(date(2026, 5, 31))

    gts = {c["group_type"] for c in out}
    assert "group" in gts and "sport" in gts
    assert "broadcast" not in gts  # genuinely unknown/irrelevant type still dropped


# ── event-anchored correlation ───────────────────────────────────────────────


async def test_fetch_upcoming_events_dedups_filters_and_is_forward_only():
    async def _fake(payload, timeout=12.0):
        return [
            {"title": "Lunch run", "date_ts": "2026-06-01T10:30:00+00:00", "group_type": "work"},
            {"title": "Lunch run", "date_ts": "2026-06-08T10:30:00+00:00", "group_type": "work"},
            {"title": "Fête", "date_ts": "2026-06-01T00:00:00+00:00", "group_type": "organisation"},
            {"title": "Past", "date_ts": "2026-05-20T10:00:00+00:00", "group_type": "work"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_upcoming_events(date(2026, 5, 31))

    # Recurring title collapses to one; non-actionable (organisation) and past
    # events are excluded.
    assert [e["title"] for e in out] == ["Lunch run"]


async def test_correlate_event_two_arms_semantic_and_lexical():
    """The query embeds the event detail and runs two retrievals: dense-only
    under the cosine floor (semantic arm) and hybrid gated by shared
    distinctive vocabulary (lexical arm). Either signal links; generic
    overlap ("achat" with a Navigo receipt) passes neither."""
    payloads: list[dict] = []

    async def _fake(payload, timeout=10.0):
        payloads.append(payload)
        if "min_score" in payload:  # the dense arm
            return [
                {
                    "id": "semantic",
                    "text": "Yep ça cours !",
                    "source": "whatsapp",
                    "date": "2026-05-31T08:03:00+00:00",
                },
            ]
        return [  # the hybrid arm — rank order, scores meaningless
            {
                "id": "noise",
                "text": "Votre justificatif d'achat Navigo",
                "source": "mail",
                "date": "2026-05-31T19:02:00+00:00",
            },
            {
                "id": "lexical",
                "text": "Sinon il faudra commander le groom sur cogeferm.fr, celui de saint laz",
                "source": "whatsapp",
                "date": "2026-05-28T09:09:00+00:00",
            },
            # The semantic arm already returned this one — dedup by id.
            {
                "id": "semantic",
                "text": "Yep ça cours !",
                "source": "whatsapp",
                "date": "2026-05-31T08:03:00+00:00",
            },
        ]

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event(
            {
                "title": "Running + achat groom",
                "detail": "achat groom gr200 ou gr300 chez cogefrem saint laz",
                "when_label": "2026-06-12 (Friday) 12:00",
            },
            after="2026-05-11",
        )

    assert out is not None and out["event"] == "Running + achat groom"
    # Lexical matches outrank the dense arm's — higher precision, small cap.
    assert [c["id"] for c in out["chunks"]] == ["lexical", "semantic"]
    assert len(payloads) == 2
    dense = next(p for p in payloads if "min_score" in p)
    hybrid = next(p for p in payloads if "min_score" not in p)
    assert dense["query"] == "Running + achat groom"  # bare title — no dilution
    # The lexical arm queries the distinctive tokens themselves.
    assert "gr200" in hybrid["query"] and "groom" in hybrid["query"]
    assert "achat" not in hybrid["query"]  # generic vocabulary stays out
    for p in (dense, hybrid):
        assert p["after"] == "2026-05-11"
    assert dense["min_score"] == day_vision._CORR_MIN_SIMILARITY


async def test_correlate_event_drops_undated_and_empty_matches():
    """Even among backend-confirmed-related chunks, an undated or empty-text one
    can't be claimed as "today's plan", so it is dropped; the dated one stays."""

    async def _fake(payload, timeout=10.0):
        return [
            {"id": "blank", "text": "   ", "source": "mail", "date": "2026-05-30T00:00:00Z"},
            {"id": "undated", "text": "apéro ce soir", "source": "whatsapp"},
            {
                "id": "keep",
                "text": "déjeuner confirmé demain",
                "source": "whatsapp",
                "date": "2026-05-31T08:03:00+00:00",
            },
        ]

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event(
            {"title": "Déjeuner", "when_label": "2026-06-01 (Monday) 12:30"},
        )

    assert out is not None
    assert [c["id"] for c in out["chunks"]] == ["keep"]


async def test_correlate_event_none_when_backend_returns_nothing():
    """When the cosine floor rejects everything server-side (a routine event with
    no genuinely-related chatter), the search comes back empty → no spurious link."""

    async def _fake(payload, timeout=10.0):
        return []

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event({"title": "Réunion", "when_label": ""})

    assert out is None


# ── surgical READINESS repair ──────────────────────────────────────────────────


async def test_condense_readiness_rewrites_figure_dump():
    """A figure-dumping READINESS line is replaced by the condensed rewrite;
    the rest of the draft is untouched."""
    draft = (
        "READINESS: Récup à 66%, HRV 72 ms, sommeil 7h48 (95%), strain 9.4 hier.\n\n"
        "OBJECTIVE: la journée.\n\nLe corps du briefing reste intact."
    )
    rewrite = "READINESS: Récupération correcte (66 %) : garde l'après-midi léger."
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(return_value=rewrite)) as llm:
        out = await day_vision._condense_readiness_line(draft, "local", "m")
    assert llm.await_count == 1
    assert out.startswith(rewrite)
    assert "Le corps du briefing reste intact." in out
    assert "HRV" not in out


async def test_condense_readiness_noop_when_line_is_a_steer():
    draft = "READINESS: Base correcte, allège l'après-midi.\n\nOBJECTIVE: x.\n\nProse."
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock()) as llm:
        out = await day_vision._condense_readiness_line(draft, "local", "m")
    llm.assert_not_awaited()
    assert out == draft


async def test_condense_readiness_keeps_original_when_rewrite_still_dumps():
    draft = "READINESS: Récup 66%, HRV 72 ms, 7h48 (95%), strain 9.4.\n\nOBJECTIVE: x.\n\nProse."
    bad_rewrite = "READINESS: 66%, 72 ms, 7h48, 95% et 9.4 — tout va bien."
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(return_value=bad_rewrite)):
        out = await day_vision._condense_readiness_line(draft, "local", "m")
    assert out == draft


async def test_condense_readiness_survives_llm_failure():
    draft = "READINESS: Récup 66%, HRV 72 ms, 7h48 (95%), strain 9.4.\n\nOBJECTIVE: x.\n\nProse."
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(side_effect=RuntimeError("down"))):
        out = await day_vision._condense_readiness_line(draft, "local", "m")
    assert out == draft


# ── back-to-back chained events (pure timestamps, keyless) ─────────────────────


async def test_chained_events_detected_from_timestamps():
    """End==start pairs surface as `chained` from pure timestamps — the
    'review ends 17:00 sharp, leadership sync starts on it' signal."""
    tz = day_vision.LOCAL_TZ
    events = [
        {
            "title": "Audit cloud",
            "start": datetime(2026, 6, 11, 15, 0, tzinfo=tz),
            "end": datetime(2026, 6, 11, 17, 0, tzinfo=tz),
            "location": "",
        },
        {
            "title": "Leadership",
            "start": datetime(2026, 6, 11, 17, 0, tzinfo=tz),
            "end": datetime(2026, 6, 11, 18, 0, tzinfo=tz),
            "location": "",
        },
        {
            "title": "Dîner",
            "start": datetime(2026, 6, 11, 20, 0, tzinfo=tz),
            "end": datetime(2026, 6, 11, 21, 0, tzinfo=tz),
            "location": "",
        },
    ]
    with (
        patch.object(
            day_vision,
            "_fetch_today_located_events",
            AsyncMock(return_value=(events, "")),
        ),
        patch.object(day_vision.enrichments, "geocode_city", AsyncMock(return_value=None)),
    ):
        out = await day_vision._compute_day_enrichments(date(2026, 6, 11), "")

    assert out["chained"] == [
        {"from": "Audit cloud", "to": "Leadership", "at": "17:00", "gap_min": 0}
    ]
    # Dîner is 2h after Leadership — not chained, so it is absent from the list.


async def test_composer_failure_degrades_to_single_pass():
    """A composer crash must fall back to the mega-prompt path, not lose the
    vision."""
    health = []
    with (
        patch.object(day_vision, "_fetch_recent_whatsapp", AsyncMock(return_value=[])),
        patch.object(day_vision, "_fetch_day_context_chunks", AsyncMock(return_value=[])),
        patch.object(day_vision, "_fetch_health_chunks", AsyncMock(return_value=health)),
        patch.object(day_vision, "_fetch_upcoming_events", AsyncMock(return_value=[])),
        patch.object(
            day_vision,
            "_compute_day_enrichments",
            AsyncMock(return_value={"weather": "", "chained": []}),
        ),
        patch.object(
            day_vision,
            "extract_day_facts",
            AsyncMock(
                return_value={
                    "physical_activities": [],
                    "partner_events": [],
                    "open_loops": [],
                    "high_priority_reminders": [],
                }
            ),
        ),
        patch.object(day_vision, "compose_vision", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(
            day_vision.runtime, "_llm_call", AsyncMock(return_value="single-pass vision")
        ) as llm,
    ):
        text, rows = await day_vision._generate_day_vision(
            "2026-06-11",
            {
                "calendar": [{"when": "10:00", "title": "demo", "group_type": "work"}],
                "reminders": [],
            },
            "local",
            "m",
            use_composer=True,
        )
    assert text == "single-pass vision"
    assert llm.await_count >= 1


# ── correlation actionability gate (chantier 2) ───────────────────────────────


async def test_correlate_event_drops_closed_avis_mail():
    """The exact 2026-06-21/-22 bug: a closed April car rental must NOT fuse into
    a June car-reminder thread. Old (61 d) + names no future date → dropped."""

    async def _fake(payload, timeout=10.0):
        if "min_score" in payload:  # dense arm
            return [
                {
                    "id": "cahors",
                    "text": "Question retrait véhicule - Agence de la Gare de Cahors",
                    "source": "mail",
                    "date": "2026-04-22T08:15:45+00:00",
                }
            ]
        return []

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event(
            {
                "title": "Prendre la Voiture du roadtrip Espagne",
                "when_label": "2026-06-22 (Monday)",
            },
            after="2026-03-24",
            day=date(2026, 6, 22),
        )

    assert out is None  # the only candidate was stale → no spurious link


async def test_correlate_event_drops_closure_marker_chunk():
    """A returned-rental confirmation carries a closure marker — dropped whatever
    its age, even inside the actionable window."""

    async def _fake(payload, timeout=10.0):
        if "min_score" in payload:
            return [
                {
                    "id": "welcome",
                    "text": "Bienvenue à la maison ! Nous espérons que vous avez apprécié votre "
                    "location et le retour du véhicule.",
                    "source": "mail",
                    "date": "2026-05-19T10:00:00+00:00",  # 34 d → inside the 45 d window
                }
            ]
        return []

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event(
            {"title": "Voiture", "when_label": "2026-06-22 (Monday)"},
            day=date(2026, 6, 22),
        )

    assert out is None


async def test_correlate_event_keeps_old_mail_naming_future_date():
    """Anti-regression (value #1): a weeks-old reservation mail that names the
    upcoming date is genuine preparatory chatter — KEEP it."""

    async def _fake(payload, timeout=10.0):
        if "min_score" in payload:
            return [
                {
                    "id": "resa",
                    "text": "Confirmation : votre location démarre le 15 juillet 2026 à Valence.",
                    "source": "mail",
                    "date": "2026-04-10T09:00:00+00:00",  # 73 d old but names July
                }
            ]
        return []

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event(
            {"title": "Roadtrip Espagne", "when_label": "2026-06-22 (Monday)"},
            day=date(2026, 6, 22),
        )

    assert out is not None and [c["id"] for c in out["chunks"]] == ["resa"]


async def test_correlate_event_keeps_recent_chunk_within_window():
    """A 40-day-old chat with no future date stays — the window (45 d) is loose
    enough to preserve a live thread (adversarial-review false-positive guard)."""

    async def _fake(payload, timeout=10.0):
        if "min_score" in payload:
            return [
                {
                    "id": "wa",
                    "text": "On loue la voiture sur place à Valence, je m'en occupe.",
                    "source": "whatsapp",
                    "date": "2026-05-13T18:00:00+00:00",  # 40 d → kept
                }
            ]
        return []

    with patch.object(day_vision, "_search_mcp_memory", AsyncMock(side_effect=_fake)):
        out = await day_vision._correlate_event(
            {"title": "Roadtrip Espagne", "when_label": "2026-06-22 (Monday)"},
            day=date(2026, 6, 22),
        )

    assert out is not None and [c["id"] for c in out["chunks"]] == ["wa"]


def test_is_stale_correlation_truth_table():
    day = date(2026, 6, 22)
    closed = {"text": "Bienvenue à la maison, merci !", "date": "2026-06-20T00:00:00Z"}
    old_no_future = {"text": "retrait véhicule agence", "date": "2026-04-22T00:00:00Z"}
    old_future = {"text": "rendez-vous le 15 juillet 2026", "date": "2026-04-10T00:00:00Z"}
    recent = {"text": "on en parle", "date": "2026-06-17T00:00:00Z"}
    undated = {"text": "on en parle"}

    assert day_vision._is_stale_correlation(closed, day) is True
    assert day_vision._is_stale_correlation(old_no_future, day) is True
    assert day_vision._is_stale_correlation(old_future, day) is False
    assert day_vision._is_stale_correlation(recent, day) is False
    assert day_vision._is_stale_correlation(undated, day) is False


# ── prose repair (chantier 3): enforce tutoiement + sobriety ──────────────────

_CLEAN_VISION = (
    "READINESS: Forme correcte, garde la journée stable.\n\n"
    "OBJECTIVE: L'agence attend ta réponse pour les véhicules.\n\n"
    "Le rendez-vous de demain est à 9h [src: mail · 21 Jun].\n\n"
    "AROUND: Quelques sujets autour de la journée.\n"
    "- Réponds à Tristan sur le road trip [src: whatsapp · 6 Jun]"
)
_FORMAL_VISION = _CLEAN_VISION.replace("ta réponse", "votre réponse")
_FILLER_VISION = _CLEAN_VISION.replace(
    "Le rendez-vous de demain est à 9h",
    "Récupère la voiture aujourd'hui sans délai, une étape critique",
)


async def test_repair_voice_noop_when_clean():
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock()) as llm:
        out = await day_vision._repair_voice(_CLEAN_VISION, "local", "m")
    assert out == _CLEAN_VISION
    llm.assert_not_awaited()  # no defect → no LLM call


async def test_repair_voice_enforces_tutoiement():
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(return_value=_CLEAN_VISION)):
        out = await day_vision._repair_voice(_FORMAL_VISION, "local", "m")
    assert "ta réponse" in out and "votre" not in out


async def test_repair_voice_strips_coach_speak_filler():
    sober = _FILLER_VISION.replace(" sans délai, une étape critique", "")
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(return_value=sober)):
        out = await day_vision._repair_voice(_FILLER_VISION, "local", "m")
    assert "sans délai" not in out and "étape critique" not in out


async def test_repair_voice_keeps_original_when_no_improvement():
    # The rewrite still vouvoie → no defect reduction → keep the original.
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(return_value=_FORMAL_VISION)):
        out = await day_vision._repair_voice(_FORMAL_VISION, "local", "m")
    assert out == _FORMAL_VISION


async def test_repair_voice_keeps_original_when_src_marker_dropped():
    # A clean rewrite that drops a [src: …] attribution is rejected.
    mangled = _CLEAN_VISION.replace(" [src: whatsapp · 6 Jun]", "")
    with patch.object(day_vision.runtime, "_llm_call", AsyncMock(return_value=mangled)):
        out = await day_vision._repair_voice(_FORMAL_VISION, "local", "m")
    assert out == _FORMAL_VISION
