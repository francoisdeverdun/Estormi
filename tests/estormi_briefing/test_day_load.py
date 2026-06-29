"""The day adviser: WHOOP parsing, day shape, and the deterministic advice."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from estormi_briefing.day.day_load import choose_advice, day_features, parse_whoop

pytestmark = pytest.mark.unit

TZ = timezone(timedelta(hours=2))

# Real WHOOP chunk shape, verbatim — note the unicode minus in the deltas.
TODAY = (
    "WHOOP — Fri 12 Jun 2026.\n"
    "Recovery 81% (green) (+14% vs avg 67%). HRV 77 ms (+6 ms vs avg 71 ms). "
    "resting HR 46 bpm (−8 bpm vs avg 54 bpm). SpO2 97.2%. skin temp 33.4°C.\n"
    "Sleep 7h42 (performance 100%, efficiency 98%). 12 disturbances. respiratory rate 15.1.\n"
    "Day: strain 0.0, 617 kcal, avg HR 45, max HR 89."
)
YESTERDAY = (
    "WHOOP — Thu 11 Jun 2026.\n"
    "Recovery 67% (green) (+0% vs avg 67%). HRV 71 ms (+0 ms vs avg 71 ms). "
    "resting HR 54 bpm (−0 bpm vs avg 54 bpm). SpO2 96.8%. skin temp 33.1°C.\n"
    "Sleep 6h55 (performance 88%, efficiency 95%). 14 disturbances. respiratory rate 15.4.\n"
    "Day: strain 9.4, 1843 kcal, avg HR 62, max HR 158."
)


def _event(title: str, start: datetime, end: datetime) -> dict:
    return {"title": title, "start": start, "end": end}


def _day_inputs() -> tuple[list[dict], list[dict]]:
    """A meeting block 09:45–17:45 with a 12:00–14:00 hole, dinner at 20:00."""
    events = [
        _event(
            "Standup",
            datetime(2026, 6, 12, 9, 45, tzinfo=TZ),
            datetime(2026, 6, 12, 11, 0, tzinfo=TZ),
        ),
        _event(
            "Revue archi",
            datetime(2026, 6, 12, 14, 0, tzinfo=TZ),
            datetime(2026, 6, 12, 17, 45, tzinfo=TZ),
        ),
        _event(
            "Dîner Léa",
            datetime(2026, 6, 12, 20, 0, tzinfo=TZ),
            datetime(2026, 6, 12, 21, 30, tzinfo=TZ),
        ),
    ]
    slots = [
        {
            "start": datetime(2026, 6, 12, 11, 0, tzinfo=TZ),
            "end": datetime(2026, 6, 12, 12, 0, tzinfo=TZ),
            "minutes": 60,
        },
        {
            "start": datetime(2026, 6, 12, 12, 0, tzinfo=TZ),
            "end": datetime(2026, 6, 12, 14, 0, tzinfo=TZ),
            "minutes": 120,
        },
    ]
    return events, slots


def _snapshot(**overrides) -> dict:
    base = {
        "recovery_pct": 81,
        "band": "green",
        "sleep_hours": 7.7,
        "sleep_performance_pct": 100,
        "sleep_efficiency_pct": 98,
        "strain_yesterday": 9.4,
    }
    base.update(overrides)
    return base


def _features(**overrides) -> dict:
    base = {
        "meeting_minutes": 300,
        "first_start": "09:45",
        "last_end": "17:45",
        "evening_event": "",
        "best_slot": {"start": "12:00", "end": "14:00", "minutes": 120},
    }
    base.update(overrides)
    return base


WORKOUT_NOTE = {"title": "Séance jambes", "text": "squat 5x5, presse, fentes bulgares"}


# ── parse_whoop ───────────────────────────────────────────────────────────────


def test_parse_whoop_full_real_sample():
    """The verbatim WHOOP pair parses fully — unicode minus included."""
    snap = parse_whoop([TODAY, YESTERDAY])
    assert snap == {
        "recovery_pct": 81,
        "band": "green",
        "sleep_hours": 7.7,
        "sleep_performance_pct": 100,
        "sleep_efficiency_pct": 98,
        "strain_yesterday": 9.4,
    }


def test_parse_whoop_strain_comes_from_second_text_only():
    """Today's still-accumulating strain (0.0) must never pose as yesterday's."""
    assert parse_whoop([TODAY])["strain_yesterday"] is None
    assert parse_whoop([TODAY, YESTERDAY])["strain_yesterday"] == 9.4


def test_parse_whoop_unicode_minus_delta_parenthetical():
    """A delta parenthetical (with U+2212) right after Recovery NN% is not a
    band — the threshold fallback kicks in and parsing doesn't choke."""
    snap = parse_whoop(["Recovery 50% (−10% vs avg 60%). Sleep 6h05."])
    assert snap["recovery_pct"] == 50
    assert snap["band"] == "yellow"
    assert snap["sleep_hours"] == 6.08


def test_parse_whoop_band_thresholds_without_parenthetical():
    assert parse_whoop(["Recovery 67%."])["band"] == "green"
    assert parse_whoop(["Recovery 66%."])["band"] == "yellow"
    assert parse_whoop(["Recovery 34%."])["band"] == "yellow"
    assert parse_whoop(["Recovery 33%."])["band"] == "red"


def test_parse_whoop_explicit_band_wins_over_thresholds():
    """The device's own call is authoritative when present."""
    assert parse_whoop(["Recovery 70% (yellow)."])["band"] == "yellow"


def test_parse_whoop_empty_and_unparseable_fields_are_none():
    assert parse_whoop([]) == {
        "recovery_pct": None,
        "band": "",
        "sleep_hours": None,
        "sleep_performance_pct": None,
        "sleep_efficiency_pct": None,
        "strain_yesterday": None,
    }
    snap = parse_whoop(["WHOOP — no metrics today."])
    assert snap["recovery_pct"] is None and snap["sleep_hours"] is None


# ── day_features ──────────────────────────────────────────────────────────────


def test_day_features_meeting_block_and_longest_slot():
    events, slots = _day_inputs()
    feats = day_features(events, slots)
    assert feats["meeting_minutes"] == 300  # 75 + 225; the dinner doesn't count
    assert feats["first_start"] == "09:45"
    assert feats["last_end"] == "17:45"  # evening event excluded from the span
    assert feats["evening_event"] == "Dîner Léa"
    # The LONGEST slot wins, not the first.
    assert feats["best_slot"] == {"start": "12:00", "end": "14:00", "minutes": 120}


def test_day_features_empty_day():
    feats = day_features([], [])
    assert feats == {
        "meeting_minutes": 0,
        "first_start": "",
        "last_end": "",
        "evening_event": "",
        "best_slot": None,
    }


def test_day_features_evening_hour_is_tunable():
    events, slots = _day_inputs()
    feats = day_features(events, slots, evening_hour=14)
    assert feats["evening_event"] == "Revue archi"  # earliest of the bucket
    assert feats["last_end"] == "11:00"


# ── choose_advice: the decision branches ──────────────────────────────────────


def test_green_with_slot_and_workout_note_is_do_workout():
    advice = choose_advice(_snapshot(), _features(), [WORKOUT_NOTE], [], "", "fr")
    assert advice is not None
    assert advice["kind"] == "do_workout"
    assert advice["slot"] == {"start": "12:00", "end": "14:00", "minutes": 120}
    assert advice["workout"]["title"] == "Séance jambes"


def test_green_with_slot_and_planned_sport_is_do_workout():
    advice = choose_advice(_snapshot(), _features(), [], ["course à pied"], "", "fr")
    assert advice["kind"] == "do_workout"
    assert advice["workout"] is None  # planned sport gates the kind, no note to cite


def test_red_is_protect_recovery_even_with_workout_material():
    advice = choose_advice(
        _snapshot(recovery_pct=25, band="red"), _features(), [WORKOUT_NOTE], ["run"], "", "fr"
    )
    assert advice["kind"] == "protect_recovery"


def test_yellow_is_light_move():
    advice = choose_advice(
        _snapshot(recovery_pct=50, band="yellow"), _features(), [WORKOUT_NOTE], [], "", "fr"
    )
    assert advice["kind"] == "light_move"


def test_green_without_slot_or_material_is_go_normal():
    no_slot = choose_advice(_snapshot(), _features(best_slot=None), [WORKOUT_NOTE], [], "", "fr")
    short = choose_advice(
        _snapshot(),
        _features(best_slot={"start": "12:00", "end": "12:30", "minutes": 30}),
        [WORKOUT_NOTE],
        [],
        "",
        "fr",
    )
    no_material = choose_advice(_snapshot(), _features(), [], [], "", "fr")
    assert no_slot["kind"] == short["kind"] == no_material["kind"] == "go_normal"


def test_no_recovery_at_all_returns_none():
    assert choose_advice(parse_whoop([]), _features(), [WORKOUT_NOTE], [], "", "fr") is None


# ── choose_advice: workout selection ──────────────────────────────────────────


def test_workout_title_match_beats_earlier_text_match():
    notes = [
        {"title": "Notes du lundi", "text": "rappel : programme sport jeudi midi"},
        {"title": "Séance jambes", "text": "squat 5x5, presse, fentes"},
    ]
    advice = choose_advice(_snapshot(), _features(), notes, [], "", "fr")
    assert advice["workout"]["title"] == "Séance jambes"


def test_workout_snippet_collapses_whitespace_and_cuts_at_word_boundary():
    long_text = "musculation  haut du corps\n" + " ".join(["développé"] * 30)
    advice = choose_advice(
        _snapshot(), _features(), [{"title": "Plan", "text": long_text}], [], "", "fr"
    )
    snippet = advice["workout"]["snippet"]
    assert "\n" not in snippet and "  " not in snippet
    assert len(snippet) <= 140
    assert not snippet.endswith("développ")  # word boundary, not a mid-word cut
    assert snippet.endswith("développé")


# ── choose_advice: facts ──────────────────────────────────────────────────────


def test_facts_fr_carry_only_parsed_values():
    """Full pipeline on the real sample: every number in the facts traces back
    to a parsed/derived value — the raw chunk's averages (67, HRV 77, HR 46)
    never leak through."""
    events, slots = _day_inputs()
    advice = choose_advice(
        parse_whoop([TODAY, YESTERDAY]),
        day_features(events, slots),
        [WORKOUT_NOTE],
        [],
        "Paris : 24°C, ensoleillé",
        "fr",
    )
    facts = advice["facts"]
    assert "récupération 81% (vert)" in facts
    assert "sommeil 7h42 (performance 100%)" in facts
    assert "créneau libre 12:00–14:00 (120 min)" in facts
    assert "réunions de 09:45 à 17:45" in facts
    assert "ce soir : Dîner Léa" in facts
    assert "strain d'hier : 9.4" in facts
    assert "séance dans tes notes : Séance jambes — squat 5x5, presse, fentes bulgares" in facts
    assert "Paris : 24°C, ensoleillé" in facts  # weather verbatim

    allowed = {
        "81",
        "7",
        "42",
        "100",  # recovery + sleep
        "12",
        "00",
        "14",
        "120",
        "09",
        "45",
        "17",  # slot + meeting span
        "9.4",  # yesterday's strain
        "5",
        "5x5",  # the workout snippet's own content
        "24",  # the weather string, included verbatim
    }
    for fact in facts:
        for number in re.findall(r"\d+(?:\.\d+)?|\d+x\d+", fact):
            assert number in allowed, f"unparsed number {number!r} in fact {fact!r}"


def test_facts_omit_what_was_not_parsed():
    """A sleepless, slotless, weatherless day yields only the recovery fact —
    no placeholder lines for absent values."""
    snap = parse_whoop(["Recovery 70%."])
    advice = choose_advice(
        snap,
        _features(best_slot=None, first_start="", last_end="", evening_event=""),
        [],
        [],
        "",
        "fr",
    )
    assert advice["facts"] == ["récupération 70% (vert)"]


def test_facts_english_for_any_other_lang():
    events, slots = _day_inputs()
    advice = choose_advice(
        parse_whoop([TODAY, YESTERDAY]), day_features(events, slots), [WORKOUT_NOTE], [], "", "en"
    )
    facts = advice["facts"]
    assert "recovery 81% (green)" in facts
    assert "sleep 7h42 (performance 100%)" in facts
    assert "free slot 12:00–14:00 (120 min)" in facts
    assert "meetings from 09:45 to 17:45" in facts
    assert "tonight: Dîner Léa" in facts
    assert "yesterday's strain: 9.4" in facts


def test_planned_flag_set_only_by_calendar_sport():
    from_notes = choose_advice(_snapshot(), _features(), [WORKOUT_NOTE], [], "", "fr")
    from_calendar = choose_advice(_snapshot(), _features(), [], ["course à pied"], "", "fr")
    assert from_notes["planned"] is False  # a note is a programme, not an appointment
    assert from_calendar["planned"] is True
