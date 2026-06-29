"""
WHOOP incremental sync.

WHOOP's natural daily unit is the *cycle* — a physiological day anchored on
sleep (wake → wake). One chunk is emitted per cycle, joining the four WHOOP
collections that overlap it:

* recovery  → recovery score, HRV (rmssd), resting HR, SpO2, skin temp
* sleep     → asleep duration, stages, performance, efficiency, respiratory rate
* cycle     → day strain, energy (kJ→kcal), avg/max HR
* workout   → per-session sport, strain, avg HR, distance

Strategy: pull the last ``WHOOP_DAYS_WINDOW`` days of every collection on each
run, join by cycle, compose a compact natural-language summary, and POST it to
``/ingest_chunk``. The window is re-pulled wholesale every run rather than
cursored, because WHOOP scores a cycle's recovery *the morning after* — a cycle
that was ``PENDING`` last night must be re-emitted once scored. ``/ingest_chunk``
is idempotent (it dedupes on ``content_hash``), so re-POSTing an unchanged day
is a cheap no-op and a late-scored day cleanly replaces its provisional chunk.

Deltas vs the window mean (recovery, HRV, resting HR, sleep hours) are folded
into the text when the window holds enough days to make an average meaningful —
that comparison is what turns a number into briefing-grade context.

Public entry point: :func:`sync` returns a dict of counts.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys as _sys
import time
from typing import Any

import httpx
import structlog

from estormi_ingestion.shared.config import mcp_url
from estormi_ingestion.shared.emit import content_base_hash
from estormi_ingestion.shared.http_client import post_chunk
from estormi_ingestion.shared.watermark import set_watermark
from memory_core.pii_filter import filter_pii

from . import auth as whoop_auth  # noqa: E402

log = structlog.get_logger()

SOURCE = "whoop"
API_BASE = "https://api.prod.whoop.com/developer"

# First-and-every-run lookback window. Driven by the Manage modal's
# historic-depth picker via WHOOP_DAYS_WINDOW; 30 days by default. WHOOP data
# is one record/day per collection, so even a wide window is a handful of pages.
DAYS_WINDOW = int(os.environ.get("WHOOP_DAYS_WINDOW", "30"))

# A window mean is only worth quoting once there are a few days behind it;
# below this the deltas are noise and are omitted.
_MIN_DAYS_FOR_BASELINE = 5

MCP_URL = mcp_url()

POST_INGESTED = "ingested"
POST_SKIPPED = "skipped"
POST_ERROR = "error"


# ─── WHOOP API ─────────────────────────────────────────────────────────────


def _iso_z(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _get(path: str, access_token: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET one page with bounded retry on 429 / 5xx (WHOOP rate-limits at 429)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    last_exc: Exception | None = None
    last_status: int | None = None
    for attempt in range(6):
        try:
            resp = httpx.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=30)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_exc = exc
            time.sleep(min(2**attempt, 30))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last_status = resp.status_code
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else float(min(2**attempt, 30))
            except ValueError:
                wait = float(min(2**attempt, 30))
            time.sleep(max(0.5, min(wait, 30)))
            continue
        resp.raise_for_status()
        return resp.json()
    if last_exc:
        raise last_exc
    raise RuntimeError(f"whoop GET {path} exhausted retries (last status {last_status})")


def _collect(path: str, access_token: str, start: str, end: str) -> list[dict[str, Any]]:
    """Page through a WHOOP collection, returning every record in the window."""
    out: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        params: dict[str, Any] = {"start": start, "end": end, "limit": 25}
        if next_token:
            # WHOOP v2 is deliberately asymmetric: the REQUEST cursor param is
            # camelCase ``nextToken`` while the RESPONSE field below is
            # snake_case ``next_token``. This is per the published API — do not
            # "fix" them to match, or pagination silently stops after page one.
            params["nextToken"] = next_token
        page = _get(path, access_token, params)
        out.extend(page.get("records", []))
        next_token = page.get("next_token")
        if not next_token:
            break
    return out


# ─── Formatting ────────────────────────────────────────────────────────────


def _parse_dt(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_day(cycle: dict[str, Any]) -> str:
    """Human day label for a cycle, honouring its recorded UTC offset."""
    start = _parse_dt(cycle.get("start"))
    if start is None:
        return ""
    offset = cycle.get("timezone_offset")  # e.g. "-05:00", "+02:00"
    if offset and len(offset) == 6:
        try:
            sign = 1 if offset[0] == "+" else -1
            delta = _dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
            start = start.astimezone(_dt.timezone(sign * delta))
        except (ValueError, TypeError):
            pass
    return start.strftime("%a %-d %b %Y")


def _hm(hours: float) -> str:
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}h{m:02d}"


def _delta(value: float | None, mean: float | None, unit: str = "") -> str:
    if value is None or mean is None:
        return ""
    diff = value - mean
    # Within rounding of the window mean — a "(+0 bpm vs avg 55 bpm)" trailer is
    # pure noise, so drop it and let the bare figure stand.
    if round(diff) == 0:
        return ""
    sign = "+" if diff >= 0 else "−"  # U+2212 minus, matches briefing typography
    return f" ({sign}{abs(diff):.0f}{unit} vs avg {mean:.0f}{unit})"


def _asleep_hours(stage: dict[str, Any]) -> float | None:
    parts = [
        stage.get("total_light_sleep_time_milli"),
        stage.get("total_slow_wave_sleep_time_milli"),
        stage.get("total_rem_sleep_time_milli"),
    ]
    if all(p is None for p in parts):
        return None
    return sum(p or 0 for p in parts) / 3_600_000


def _recovery_band(score: float | None) -> str:
    if score is None:
        return ""
    if score >= 67:
        return "green"
    if score >= 34:
        return "yellow"
    return "red"


def _compose(
    cycle: dict[str, Any],
    recovery: dict[str, Any] | None,
    sleep: dict[str, Any] | None,
    workouts: list[dict[str, Any]],
    baseline: dict[str, float | None],
) -> str:
    """Render one cycle + its joined data into the chunk text."""
    lines: list[str] = [f"WHOOP — {_local_day(cycle)}."]

    rec = (recovery or {}).get("score") or {}
    if rec:
        rscore = rec.get("recovery_score")
        band = _recovery_band(rscore)
        hrv = rec.get("hrv_rmssd_milli")
        rhr = rec.get("resting_heart_rate")
        seg = []
        if rscore is not None:
            seg.append(
                f"Recovery {rscore:.0f}%{(' (' + band + ')') if band else ''}"
                f"{_delta(rscore, baseline.get('recovery'), '%')}"
            )
        if hrv is not None:
            seg.append(f"HRV {hrv:.0f} ms{_delta(hrv, baseline.get('hrv'), ' ms')}")
        if rhr is not None:
            seg.append(f"resting HR {rhr:.0f} bpm{_delta(rhr, baseline.get('rhr'), ' bpm')}")
        if rec.get("spo2_percentage") is not None:
            seg.append(f"SpO2 {rec['spo2_percentage']:.1f}%")
        if rec.get("skin_temp_celsius") is not None:
            seg.append(f"skin temp {rec['skin_temp_celsius']:.1f}°C")
        if seg:
            lines.append(". ".join(seg) + ".")

    slp = (sleep or {}).get("score") or {}
    if slp:
        stage = slp.get("stage_summary") or {}
        asleep = _asleep_hours(stage)
        seg = []
        if asleep is not None:
            perf = slp.get("sleep_performance_percentage")
            eff = slp.get("sleep_efficiency_percentage")
            extra = []
            if perf is not None:
                extra.append(f"performance {perf:.0f}%")
            if eff is not None:
                extra.append(f"efficiency {eff:.0f}%")
            paren = f" ({', '.join(extra)})" if extra else ""
            seg.append(f"Sleep {_hm(asleep)}{paren}{_delta(asleep, baseline.get('sleep'), 'h')}")
        if stage.get("disturbance_count") is not None:
            seg.append(f"{stage['disturbance_count']} disturbances")
        if slp.get("respiratory_rate") is not None:
            seg.append(f"respiratory rate {slp['respiratory_rate']:.1f}")
        if seg:
            lines.append(". ".join(seg) + ".")

    cyc = cycle.get("score") or {}
    if cyc:
        seg = []
        if cyc.get("strain") is not None:
            seg.append(f"strain {cyc['strain']:.1f}")
        if cyc.get("kilojoule") is not None:
            seg.append(f"{cyc['kilojoule'] * 0.239006:.0f} kcal")
        if cyc.get("average_heart_rate") is not None:
            seg.append(f"avg HR {cyc['average_heart_rate']:.0f}")
        if cyc.get("max_heart_rate") is not None:
            seg.append(f"max HR {cyc['max_heart_rate']:.0f}")
        if seg:
            lines.append("Day: " + ", ".join(seg) + ".")

    if workouts:
        items = []
        for w in workouts:
            wscore = w.get("score") or {}
            sport = w.get("sport_name") or "workout"
            start = _parse_dt(w.get("start"))
            end = _parse_dt(w.get("end"))
            dur = ""
            if start and end:
                dur = f" {int((end - start).total_seconds() // 60)} min"
            bits = [f"{sport}{dur}"]
            if wscore.get("strain") is not None:
                bits.append(f"strain {wscore['strain']:.1f}")
            if wscore.get("average_heart_rate") is not None:
                bits.append(f"avg HR {wscore['average_heart_rate']:.0f}")
            if wscore.get("distance_meter"):
                bits.append(f"{wscore['distance_meter'] / 1000:.1f} km")
            label = bits[0]
            if bits[1:]:
                label += " (" + ", ".join(bits[1:]) + ")"
            items.append(label)
        lines.append("Workouts: " + "; ".join(items) + ".")

    return "\n".join(lines)


# ─── Join + baseline ───────────────────────────────────────────────────────


def _index_by_cycle(
    records: list[dict[str, Any]], key: str = "cycle_id"
) -> dict[Any, dict[str, Any]]:
    out: dict[Any, dict[str, Any]] = {}
    for r in records:
        cid = r.get(key)
        if cid is not None and cid not in out:
            out[cid] = r
    return out


def _baseline(
    cycles: list[dict[str, Any]],
    recoveries_by_cycle: dict[Any, dict[str, Any]],
    sleeps_by_cycle: dict[Any, dict[str, Any]],
) -> dict[str, float | None]:
    """Window means used for the inline deltas. ``None`` when too few days."""

    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if len(values) >= _MIN_DAYS_FOR_BASELINE else None

    rscores, hrvs, rhrs, sleeps = [], [], [], []
    for c in cycles:
        rec = (recoveries_by_cycle.get(c.get("id")) or {}).get("score") or {}
        if rec.get("recovery_score") is not None:
            rscores.append(rec["recovery_score"])
        if rec.get("hrv_rmssd_milli") is not None:
            hrvs.append(rec["hrv_rmssd_milli"])
        if rec.get("resting_heart_rate") is not None:
            rhrs.append(rec["resting_heart_rate"])
        stage = ((sleeps_by_cycle.get(c.get("id")) or {}).get("score") or {}).get(
            "stage_summary"
        ) or {}
        hours = _asleep_hours(stage)
        if hours is not None:
            sleeps.append(hours)
    return {
        "recovery": _mean(rscores),
        "hrv": _mean(hrvs),
        "rhr": _mean(rhrs),
        "sleep": _mean(sleeps),
    }


def _workouts_for_cycle(
    cycle: dict[str, Any], workouts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    start = _parse_dt(cycle.get("start"))
    end = _parse_dt(cycle.get("end")) or _dt.datetime.now(_dt.timezone.utc)
    if start is None:
        return []
    out = []
    for w in workouts:
        ws = _parse_dt(w.get("start"))
        if ws is not None and start <= ws < end:
            out.append(w)
    return out


def _post_cycle(cycle: dict[str, Any], text: str) -> str:
    """POST one composed cycle to /ingest_chunk. Mirrors gcal._post_event."""
    cycle_id = cycle.get("id")
    source_id = f"whoop-cycle-{cycle_id}"
    # The composed text is numeric/templated physiological data, but we still
    # run the PII pass so the `pii_filtered: True` flag below is honest and
    # the server can safely skip its own filter — same contract as every other
    # connector.
    text = filter_pii(text)
    # Fold source_id in (content_base_hash): the server dedups GLOBALLY on
    # content_hash, so two rest/no-data days whose templated summary renders
    # byte-identical would otherwise collide and the second day be dropped.
    content_hash = content_base_hash(source_id, text)
    day = _local_day(cycle)
    title = f"WHOOP — {day}" if day else "WHOOP cycle"
    try:
        resp = post_chunk(
            f"{MCP_URL}/ingest_chunk",
            {
                "text": text,
                "source": SOURCE,
                "source_id": source_id,
                "title": title,
                "date": cycle.get("start"),
                "content_hash": content_hash,
                # Health is always the user's own life context. "me" is the
                # canonical group_type for that (services.calendar_oauth
                # .GCAL_GROUP_TYPES / the MCP enum) — so a group_type="me"
                # search surfaces WHOOP data alongside the user's own calendar.
                "group_type": "me",
                "meta": {"pii_filtered": True},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return POST_SKIPPED if resp.json().get("status") == "skipped" else POST_INGESTED
    except Exception as exc:  # noqa: BLE001 — one bad day must not abort the run
        log.warning("whoop: ingest failed for %s: %s", source_id, exc)
        return POST_ERROR


def sync(**kwargs) -> dict[str, int]:
    """Run a full-window sync. Returns counts dict."""
    counts = {"ingested": 0, "skipped": 0, "cycles": 0, "errors": 0}

    access_token = whoop_auth.get_access_token()
    if access_token is None:
        log.warning("no whoop credentials available")
        counts["errors"] = 1
        return counts

    now = _dt.datetime.now(_dt.timezone.utc)
    start = _iso_z(now - _dt.timedelta(days=DAYS_WINDOW))
    end = _iso_z(now + _dt.timedelta(days=1))

    cycles = _collect("/v2/cycle", access_token, start, end)
    recoveries = _collect("/v2/recovery", access_token, start, end)
    sleeps = _collect("/v2/activity/sleep", access_token, start, end)
    workouts = _collect("/v2/activity/workout", access_token, start, end)

    recoveries_by_cycle = _index_by_cycle(recoveries)
    # A cycle owns one main sleep + zero or more naps; keep the first
    # non-nap sleep as the night's record.
    sleeps_by_cycle: dict[Any, dict[str, Any]] = {}
    for s in sleeps:
        cid = s.get("cycle_id")
        if cid is None or s.get("nap"):
            continue
        sleeps_by_cycle.setdefault(cid, s)

    baseline = _baseline(cycles, recoveries_by_cycle, sleeps_by_cycle)

    last_cycle_id: str | None = None
    for cycle in cycles:
        cid = cycle.get("id")
        text = _compose(
            cycle,
            recoveries_by_cycle.get(cid),
            sleeps_by_cycle.get(cid),
            _workouts_for_cycle(cycle, workouts),
            baseline,
        )
        outcome = _post_cycle(cycle, text)
        if outcome == POST_INGESTED:
            counts["ingested"] += 1
        elif outcome == POST_SKIPPED:
            counts["skipped"] += 1
        elif outcome == POST_ERROR:
            counts["errors"] += 1
        counts["cycles"] += 1
        last_cycle_id = str(cid)

    # Watermark is informational here (the window is always re-pulled) — it
    # drives the "last sync" the Settings UI shows. Persist only on a clean
    # run so a failed sync doesn't advance the displayed timestamp.
    if counts["errors"] == 0:
        import asyncio  # noqa: PLC0415

        asyncio.run(set_watermark(SOURCE, _iso_z(now), last_cycle_id))

    return counts


# ─── Wake probe ──────────────────────────────────────────────────────────────
#
# Used by the morning "wake trigger" poller (server.jobs._schedule_whoop_poll):
# a read-only check for "has WHOOP scored a recovery for the night that just
# ended?". WHOOP only scores recovery once it has processed the completed
# sleep — i.e. shortly after the user wakes — so a freshly-scored recovery is
# the closest reliable proxy for "awake". This ingests nothing; it just peeks.


def recovery_available_today() -> str | None:
    """Local date (YYYY-MM-DD) of the most recent *scored* recovery, else None.

    Pulls the last ~36 h of ``/v2/recovery`` (cheap — one record per night)
    and returns the local-time date on which WHOOP scored the newest record
    that actually has a ``recovery_score``. The poller compares this to the
    local "today" to decide whether the user has woken and been scored. Any
    error (no token, API hiccup) yields ``None`` so the poller simply waits
    for the next tick.
    """
    access_token = whoop_auth.get_access_token()
    if access_token is None:
        return None
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        start = _iso_z(now - _dt.timedelta(hours=36))
        end = _iso_z(now + _dt.timedelta(hours=1))
        recoveries = _collect("/v2/recovery", access_token, start, end)
    except Exception as exc:  # noqa: BLE001 — a transient probe failure is not fatal
        log.warning("whoop: recovery probe failed: %s", exc)
        return None

    newest_dt: _dt.datetime | None = None
    for rec in recoveries:
        score = rec.get("score") or {}
        if score.get("recovery_score") is None:
            continue
        scored_at = _parse_dt(rec.get("created_at") or rec.get("updated_at"))
        if scored_at is not None and (newest_dt is None or scored_at > newest_dt):
            newest_dt = scored_at

    if newest_dt is None:
        return None
    # Convert the UTC scoring time to the host's local day — that is the day
    # the poller's window and "already fired" guard are expressed in.
    return newest_dt.astimezone().strftime("%Y-%m-%d")


# ─── CLI entry point ────────────────────────────────────────────────────────
#
# Invoked by the per-source ▶ play button in the SPA and by a scoped pipeline run.
# Prints a one-line summary and exits non-zero on any error so the stage is
# marked failed.

# Exit code reserved for "stored refresh token is dead — re-auth required".
_EXIT_REAUTH = 2


def _main() -> int:
    # No usable token (nothing stored, or refresh token revoked) surfaces as
    # get_access_token() → None inside sync(); distinguish the revoked case so
    # a future UI layer can tell "needs re-auth" from "transient API error".
    if whoop_auth.load_token() is not None and whoop_auth.get_access_token() is None:
        print(
            "[whoop] refresh token expired or revoked — reconnect via Settings → WHOOP.",
            flush=True,
        )
        return _EXIT_REAUTH
    counts = sync()
    print(
        f"[whoop] cycles={counts['cycles']} ingested={counts['ingested']} "
        f"skipped={counts['skipped']} errors={counts['errors']}"
    )
    return 1 if counts.get("errors") else 0


if __name__ == "__main__":
    _sys.exit(_main())
