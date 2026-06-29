"""Code-decided day adviser: WHOOP parsing, day shape, and the recommendation.

The briefing's READINESS line used to be a phrasing exercise over raw health
rows; this module upgrades it into an adviser. Code parses the WHOOP chunk
texts and the day's calendar shape, decides the recommendation
deterministically, and hands the LLM writer a small set of FACTS it may
phrase from. The LLM never decides — and every number a fact carries is
parsed or derived here, so the composer's downstream verification (a number
that doesn't trace to a data row drops the whole line) always finds a source.

Three pure functions, no I/O:

* :func:`parse_whoop` — the newest WHOOP rows → a recovery/sleep snapshot;
* :func:`day_features` — calendar events + free slots → the day's shape;
* :func:`choose_advice` — snapshot + shape + workout material → the
  recommendation kind and its facts.
"""

from __future__ import annotations

import re
from datetime import datetime

# WHOOP renders deltas with the unicode minus ("−8 bpm vs avg"); normalise it
# to ASCII before matching so no regex has to special-case U+2212.
_UNICODE_MINUS = "−"

_RECOVERY_RE = re.compile(r"Recovery\s+(\d{1,3})\s*%(?:\s*\((green|yellow|red)\))?", re.IGNORECASE)
_SLEEP_RE = re.compile(r"Sleep\s+(\d{1,2})\s*h\s*(\d{1,2})?", re.IGNORECASE)
_PERFORMANCE_RE = re.compile(r"performance\s+(\d{1,3})\s*%", re.IGNORECASE)
_EFFICIENCY_RE = re.compile(r"efficiency\s+(\d{1,3})\s*%", re.IGNORECASE)
_STRAIN_RE = re.compile(r"Day:\s*strain\s+(\d+(?:\.\d+)?)", re.IGNORECASE)

# WHOOP's own recovery bands, used when the chunk omits the parenthetical.
_GREEN_FLOOR = 67
_YELLOW_FLOOR = 34

# A workout-like note: the vocabulary the user actually writes plans in.
_WORKOUT_RE = re.compile(
    r"séance|musculation|renfo|workout|training|programme sport", re.IGNORECASE
)
_SNIPPET_LIMIT = 140

# A slot must fit a real session, warm-up included — below this the adviser
# doesn't push a workout into it.
_MIN_WORKOUT_SLOT_MINUTES = 45

_BAND_WORDS_FR = {"green": "vert", "yellow": "jaune", "red": "rouge"}


def _band_for(recovery_pct: int) -> str:
    if recovery_pct >= _GREEN_FLOOR:
        return "green"
    if recovery_pct >= _YELLOW_FLOOR:
        return "yellow"
    return "red"


def parse_whoop(texts: list[str]) -> dict:
    """Parse the newest WHOOP rows into a snapshot dict.

    texts: chunk texts, NEWEST FIRST (today's cycle first, yesterday's second).
    Returns {"recovery_pct": int|None, "band": "green"|"yellow"|"red"|"",
             "sleep_hours": float|None, "sleep_performance_pct": int|None,
             "sleep_efficiency_pct": int|None, "strain_yesterday": float|None}.

    Today's recovery and sleep come from the FIRST text; yesterday's strain
    from the SECOND text's "Day: strain N.N" line (today's cycle is still
    accumulating, so its strain says nothing about what the body absorbed).
    The band is the parenthetical after "Recovery NN%" when present, else
    WHOOP's thresholds (>= 67 green, >= 34 yellow, else red). Any field the
    text doesn't carry parses to None — never a guessed default.
    """
    today = (texts[0] if texts else "").replace(_UNICODE_MINUS, "-")
    yesterday = (texts[1] if len(texts) > 1 else "").replace(_UNICODE_MINUS, "-")

    recovery_pct: int | None = None
    band = ""
    if m := _RECOVERY_RE.search(today):
        recovery_pct = int(m.group(1))
        band = (m.group(2) or _band_for(recovery_pct)).lower()

    sleep_hours: float | None = None
    if m := _SLEEP_RE.search(today):
        # "Sleep 7h42" → 7.70; two decimals so the facts can reconstruct the
        # exact NhMM the chunk carried (one decimal would drift 7h25 to 7h24).
        sleep_hours = round(int(m.group(1)) + int(m.group(2) or 0) / 60, 2)

    perf = _PERFORMANCE_RE.search(today)
    eff = _EFFICIENCY_RE.search(today)
    strain = _STRAIN_RE.search(yesterday)
    return {
        "recovery_pct": recovery_pct,
        "band": band,
        "sleep_hours": sleep_hours,
        "sleep_performance_pct": int(perf.group(1)) if perf else None,
        "sleep_efficiency_pct": int(eff.group(1)) if eff else None,
        "strain_yesterday": float(strain.group(1)) if strain else None,
    }


def _hhmm(value: datetime | str) -> str:
    """%H:%M for a datetime; a pre-formatted slot bound passes through."""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    return str(value)


def day_features(events: list[dict], slots: list[dict], *, evening_hour: int = 19) -> dict:
    """Code-derived shape of the day.

    events: [{"title": str, "start": datetime, "end": datetime}] tz-aware,
    in the local timezone; slots: free_slots() output
    [{"start", "end", "minutes"}].
    Returns {"meeting_minutes": int, "first_start": "09:45"|"",
             "last_end": "17:45"|"", "evening_event": str (title or ""),
             "best_slot": {"start":"12:00","end":"14:00","minutes":120}|None}.
    best_slot = the LONGEST slot; times %H:%M local.

    Events starting at/after ``evening_hour`` are the evening bucket: the
    earliest one's title becomes ``evening_event``, and they are excluded
    from ``meeting_minutes`` / ``first_start`` / ``last_end`` — "réunions de
    09:45 à 17:45" must describe the working block, not stretch to a dinner.
    """
    daytime = [e for e in events if e["start"].hour < evening_hour]
    evening = [e for e in events if e["start"].hour >= evening_hour]

    meeting_minutes = int(sum((e["end"] - e["start"]).total_seconds() for e in daytime) // 60)
    first_start = min((e["start"] for e in daytime), default=None)
    last_end = max((e["end"] for e in daytime), default=None)
    evening_event = min(evening, key=lambda e: e["start"])["title"] if evening else ""

    best = max(slots, key=lambda s: s["minutes"], default=None)
    best_slot = None
    if best is not None:
        best_slot = {
            "start": _hhmm(best["start"]),
            "end": _hhmm(best["end"]),
            "minutes": int(best["minutes"]),
        }
    return {
        "meeting_minutes": meeting_minutes,
        "first_start": _hhmm(first_start) if first_start else "",
        "last_end": _hhmm(last_end) if last_end else "",
        "evening_event": evening_event,
        "best_slot": best_slot,
    }


def _snippet(text: str, limit: int = _SNIPPET_LIMIT) -> str:
    """First ``limit`` chars, whitespace-collapsed, cut at a word boundary."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    head, _, _ = cut.rpartition(" ")
    return (head or cut).rstrip()


def _pick_workout(workout_notes: list[dict]) -> dict | None:
    """The most workout-like note — a title match beats any text match.

    A plan named "Séance jambes" is the session itself; a note that merely
    mentions training in passing is weaker evidence, so title matches win
    regardless of list order.
    """
    chosen = next(
        (n for n in workout_notes if _WORKOUT_RE.search(n.get("title") or "")), None
    ) or next((n for n in workout_notes if _WORKOUT_RE.search(n.get("text") or "")), None)
    if chosen is None:
        return None
    return {
        "title": (chosen.get("title") or "").strip(),
        "snippet": _snippet(chosen.get("text") or ""),
    }


def _sleep_label(hours: float) -> str:
    """7.7 → "7h42" — back to the NhMM form the WHOOP chunk carried."""
    h = int(hours)
    m = round((hours - h) * 60)
    if m == 60:  # float dust right under a whole hour
        h, m = h + 1, 0
    return f"{h}h{m:02d}"


def _build_facts(
    snapshot: dict,
    features: dict,
    band: str,
    workout: dict | None,
    weather: str,
    lang: str,
) -> list[str]:
    """Short declarative facts, every number traceable to a parsed value."""
    fr = lang == "fr"
    facts: list[str] = []

    recovery = snapshot.get("recovery_pct")
    band_word = _BAND_WORDS_FR[band] if fr else band
    facts.append(
        f"récupération {recovery}% ({band_word})" if fr else f"recovery {recovery}% ({band_word})"
    )

    sleep_hours = snapshot.get("sleep_hours")
    if sleep_hours is not None:
        label = _sleep_label(sleep_hours)
        perf = snapshot.get("sleep_performance_pct")
        suffix = f" (performance {perf}%)" if perf is not None else ""
        facts.append((f"sommeil {label}" if fr else f"sleep {label}") + suffix)

    slot = features.get("best_slot")
    if slot:
        facts.append(
            f"créneau libre {slot['start']}–{slot['end']} ({slot['minutes']} min)"
            if fr
            else f"free slot {slot['start']}–{slot['end']} ({slot['minutes']} min)"
        )

    first_start, last_end = features.get("first_start"), features.get("last_end")
    if first_start and last_end:
        facts.append(
            f"réunions de {first_start} à {last_end}"
            if fr
            else f"meetings from {first_start} to {last_end}"
        )

    if evening := features.get("evening_event"):
        facts.append(f"ce soir : {evening}" if fr else f"tonight: {evening}")

    if workout:
        facts.append(
            f"séance dans tes notes : {workout['title']} — {workout['snippet']}"
            if fr
            else f"workout in your notes: {workout['title']} — {workout['snippet']}"
        )

    strain = snapshot.get("strain_yesterday")
    if strain is not None:
        # .1f matches WHOOP's own one-decimal rendering — no invented digits.
        facts.append(f"strain d'hier : {strain:.1f}" if fr else f"yesterday's strain: {strain:.1f}")

    if weather:
        facts.append(weather)
    return facts


def choose_advice(
    snapshot: dict,
    features: dict,
    workout_notes: list[dict],
    planned_sport: list[str],
    weather: str,
    lang: str,
) -> dict | None:
    """Deterministic recommendation + the facts the writer may phrase from.

    Decision rules, in band order (the bands are mutually exclusive):

    * no recovery in the snapshot → ``None`` — without the load-bearing
      number there is nothing to advise from;
    * band red → ``protect_recovery``;
    * band yellow → ``light_move``;
    * band green AND a best_slot of >= 45 minutes AND workout material
      (a workout note or a planned sport) → ``do_workout``;
    * band green otherwise (no slot, slot too short, or nothing planned)
      → ``go_normal``.

    Returns {"kind": ..., "slot": features["best_slot"],
    "workout": {"title", "snippet"}|None, "planned": bool,
    "facts": list[str]}. ``facts`` are
    short declarative strings in ``lang`` ("fr", anything else → English),
    built ONLY from parsed/derived values — the composer verifies every
    number downstream, and an invented one drops the whole line.

    ``planned`` is True only when the CALENDAR carries a sport activity
    (``planned_sport``, mined by the extractor). A workout note is a
    programme the user keeps, not a commitment on the day — the READINESS
    writer may only claim a session is « prévue » when ``planned`` is True
    (a notes-only day shipped « ta séance de musculation prévue » in
    production, flagged by the fact-critic and never repaired).
    """
    recovery = snapshot.get("recovery_pct")
    if recovery is None:
        return None
    band = snapshot.get("band") or _band_for(recovery)

    slot = features.get("best_slot")
    workout = _pick_workout(workout_notes)
    if band == "red":
        kind = "protect_recovery"
    elif band == "yellow":
        kind = "light_move"
    elif (
        slot is not None
        and slot["minutes"] >= _MIN_WORKOUT_SLOT_MINUTES
        and (workout_notes or planned_sport)
    ):
        kind = "do_workout"
    else:
        kind = "go_normal"

    return {
        "kind": kind,
        "slot": slot,
        "workout": workout,
        "planned": bool(planned_sport),
        "facts": _build_facts(snapshot, features, band, workout, weather, lang),
    }
