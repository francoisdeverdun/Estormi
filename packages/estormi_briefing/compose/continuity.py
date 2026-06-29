"""Day-to-day continuity — the "↩ yesterday's briefing was preparing X" callback.

A daily briefing reads better as a serial than as disconnected snapshots:
when yesterday's edition spent a thread preparing an event and that event is
on today's calendar, the natural opening is a callback, not a cold
re-introduction. The composer persists what today's edition prepared (its
thread titles + lede) into a small JSON state file under the data dir, and
the next morning's run turns any prepared topic that materialised on the
calendar into a one-line callback.

Deterministic code only — no LLM. Matching reuses ``normalised_key`` from
``fact_lint`` (the one shared dedup normaliser) over a 24-char prefix, so
the title stored yesterday still matches today's calendar row when only the
suffix differs (room, attendee, time tacked on). State handling follows the
``_decay_seen_around`` pattern in ``composer.py``: missing/corrupt state is
an empty start, persistence is best-effort, and nothing here ever raises —
continuity is a garnish, never a reason to fail a briefing.
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path

import structlog

from estormi_briefing.lint.fact_lint import normalised_key

log = structlog.get_logger()

STATE_FILE = "briefing_continuity.json"

# A briefing rarely prepares more than a handful of topics; capping what we
# store keeps the state file (and tomorrow's matching loop) trivially small.
_MAX_THREADS = 8
_MAX_LEDE = 300
# More than a couple of "remember yesterday?" lines stops being continuity
# and starts being a recap — the briefing body already covers today.
_MAX_CALLBACKS = 2
# Long enough that two distinct meetings rarely collide, short enough that a
# suffix added by the calendar (room, attendee) doesn't break the match.
_KEY_PREFIX = 24


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILE


def load_state(data_dir: Path) -> dict:
    """Parsed state or {} — missing/corrupt/wrong-shape never raises."""
    try:
        state = json.loads(_state_path(data_dir).read_text())
        return state if isinstance(state, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt state is an empty start
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    """Best-effort JSON write (mkdir parents, ensure_ascii=False); never raises."""
    try:
        path = _state_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — persistence is best-effort
        log.warning("continuity: state not persisted")


def build_state(date_str: str, thread_titles: list[str], lede: str) -> dict:
    """{"date": date_str[:10], "threads": [up to 8 cleaned titles], "lede": lede[:300]}.

    Titles are stripped, empties dropped, and case-insensitive duplicates
    removed (the composer can emit the same topic under two threads) — the
    first spelling wins so tomorrow's callback shows what the user saw.
    """
    threads: list[str] = []
    seen: set[str] = set()
    for raw in thread_titles:
        title = (raw or "").strip()
        if not title or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        threads.append(title)
        if len(threads) >= _MAX_THREADS:
            break
    return {"date": date_str[:10], "threads": threads, "lede": (lede or "")[:_MAX_LEDE]}


def callbacks(state: dict, date_str: str, calendar: list[dict], lang: str) -> list[str]:
    """Max 2 localized callback lines for today, [] unless state is from EXACTLY
    yesterday (date math, not string math — a Friday briefing must not call
    back to Wednesday as if it were yesterday, and a stale month-old state
    file must stay silent)."""
    try:
        today = _date.fromisoformat(str(date_str)[:10])
        prepared_on = _date.fromisoformat(str(state.get("date") or "")[:10])
    except (TypeError, ValueError):
        return []
    if today - prepared_on != _timedelta(days=1):
        return []

    prepared: set[str] = set()
    for title in state.get("threads") or []:
        if isinstance(title, str):
            key = normalised_key(title)[:_KEY_PREFIX]
            if key:
                prepared.add(key)
    if not prepared:
        return []

    lines: list[str] = []
    emitted: set[str] = set()
    for row in calendar:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        key = normalised_key(title)[:_KEY_PREFIX]
        if not key or key in emitted or key not in prepared:
            continue
        emitted.add(key)
        # The calendar row is the current truth — its title (not yesterday's
        # stored phrasing) is what the rest of today's briefing will use.
        when = str(row.get("when") or "").strip()
        timed = bool(when) and when.casefold() != "all day"
        if lang == "fr":
            line = f"↩ Hier, le briefing préparait « {title} » — c'est aujourd'hui"
            line += f" à {when}." if timed else "."
        else:
            line = f"↩ Yesterday's briefing was preparing “{title}” — that's today"
            line += f" at {when}." if timed else "."
        lines.append(line)
        if len(lines) >= _MAX_CALLBACKS:
            break
    return lines
