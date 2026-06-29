"""Vault-metrics reporting for the iOS companion.

After each engine finishes, ``jobs`` calls in here to (best-effort) append a row
to the rolling engine history (``engines_history.json``) and refresh the full
metrics snapshot the companion's Metrics page reads (total chunks, per-source
composition, the ingestion + memory time series, and the read-only source
catalogue). Everything is failure-tolerant: a snapshot miss must never affect
the engine lifecycle.

This belongs in estormi_server, not ``estormi_ingestion.shared.delivery.vault_sync``: it reads SQLite
and the connector registry, which the pure file-writer must not depend on. The
queue/mutex and process lifecycle stay in ``jobs``; the log-slice reader, the
settings snapshot, and the connector registry are referenced through ``jobs`` at
call time so the test suite's ``patch.object(jobs, …)`` hooks keep applying.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from estormi_server.server import jobs as _jobs
from memory_core.timeparse import parse_iso

log = structlog.get_logger()

# Sources whose watermark hasn't advanced in this many days are flagged on the
# ingestion run record so the UI / companion can surface a silently-frozen
# connector.
_WATERMARK_STALE_DAYS = 7

# How many days of history the companion's stacked-area charts cover, matching
# the macOS IngestionPulse strip ("14 days · by source").
_VAULT_METRICS_WINDOW_DAYS = 14


async def _stale_watermarks(threshold_days: int) -> list[dict[str, Any]]:
    """Sources whose ``last_fetched_at`` is older than ``threshold_days``.

    Returns a list of ``{"source": str, "age_days": int}`` sorted by age
    (oldest first). Sources without a timestamp watermark (gcal, whatsapp,
    briefing — see ``WATERMARK_MECHANISM`` in the UI) are absent from the
    table and therefore from the result.
    """
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    db = sqlite_conn()
    cur = await db.execute("SELECT source, last_fetched_at FROM ingestion_watermarks")
    rows = await cur.fetchall()
    await cur.close()
    now = datetime.now(timezone.utc)
    stale: list[dict[str, Any]] = []
    for source, ts in rows:
        when = parse_iso(ts)
        if when is None:
            continue
        age = (now - when).days
        if age >= threshold_days:
            stale.append({"source": source, "age_days": age})
    stale.sort(key=lambda s: s["age_days"], reverse=True)
    return stale


async def _record_engine_run(
    engine: str,
    started_at: float,
    ended_at: float,
    status: str,
    vault_sync_failed: bool = False,
    log_slices: list[tuple[str, int, str]] | None = None,
) -> None:
    """Capture the current state of ``engine`` and append a row to the
    rolling vault history (``engines_history.json``).

    Runs at the end of each engine — best-effort: a snapshot failure must
    never affect engine lifecycle. The counters mirror what each engine's
    ``/status`` endpoint already returns so the iOS companion can plot the
    same numbers the desktop UI shows, but as a time series instead of a
    point-in-time snapshot.
    """
    try:
        from estormi_ingestion.shared.delivery.vault_sync import (  # noqa: PLC0415
            push_engine_log,
            push_engine_run,
        )

        started_iso = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        ended_iso = datetime.fromtimestamp(ended_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        counters: dict[str, Any] = {}
        if engine == "ingestion":
            # Chunks added by this run, grouped by source. Cheap and direct —
            # avoids depending on /api/pipeline's heavier dataclass walk.
            try:
                from estormi_server.services.pipeline_status import (
                    _chunks_added_since,  # noqa: PLC0415
                )

                total, by_source = _chunks_added_since(
                    datetime.fromtimestamp(started_at, tz=timezone.utc)
                )
                counters = {
                    "chunks_added": total,
                    "by_source": by_source,
                }
            except Exception:
                log.exception("vault_history.ingestion_counters_failed")
            # Sources that haven't advanced in a while. A connector that keeps
            # erroring (failed > 0) leaves its watermark pinned and never
            # surfaces a signal — the engine reports "ok" run after run while
            # the source is silently frozen. Anything older than the staleness
            # threshold gets logged and dropped on the run record so the UI
            # and iOS companion can flag it. Sources with non-timestamp
            # progress (gcal sync tokens, etc.) live outside this table and
            # are intentionally skipped.
            try:
                stale = await _stale_watermarks(_WATERMARK_STALE_DAYS)
                if stale:
                    log.warning("watermark.stale", sources=stale)
                    counters["stale_watermarks"] = stale
            except Exception:
                log.exception("vault_history.stale_watermark_probe_failed")
        elif engine == "briefing":
            from estormi_ingestion.shared.delivery.vault_sync import vault_dir  # noqa: PLC0415

            # vault_dir() lives in iCloud Drive — glob + is_dir() are blocking
            # filesystem calls; keep them off the event loop.
            def _list_briefings() -> list[str]:
                briefings_dir = vault_dir() / "briefings"
                return (
                    sorted(p.stem for p in briefings_dir.glob("*.json"))
                    if briefings_dir.is_dir()
                    else []
                )

            files = await asyncio.to_thread(_list_briefings)
            counters = {
                "briefings_total": len(files),
                "last_date": files[-1] if files else None,
            }

        payload: dict[str, Any] = {
            "engine": engine,
            "startedAt": started_iso,
            "endedAt": ended_iso,
            "durationMs": max(0, int((ended_at - started_at) * 1000)),
            "status": status,
            "counters": counters,
        }
        # Engine itself succeeded but the vault snapshot didn't — let the UI
        # and iOS companion surface "stale" on this run instead of pretending
        # the on-disk catalogue is current.
        if vault_sync_failed:
            payload["vaultSyncFailed"] = True

        # This run's slice of each rolling engine log, written to its own
        # ``engine-logs/<run_id>.log`` file so the companion can fetch the full
        # output on demand instead of bloating the history index. ``run_id`` is
        # derived from engine + start instant so it's stable and unique per run.
        if log_slices:
            sections: list[str] = []
            for path, start, label in log_slices:
                # Log files can be large; the slice read is blocking I/O.
                text = await asyncio.to_thread(_jobs._read_log_slice, path, start)
                if not text.strip():
                    continue
                sections.append(f"── {label} ──\n{text}" if label else text)
            logs = "\n".join(sections).strip()
            if logs:
                run_id = f"{engine}-{started_iso.replace(':', '').replace('-', '')}"
                if await asyncio.to_thread(push_engine_log, run_id, logs):
                    payload["logId"] = run_id

        await asyncio.to_thread(push_engine_run, payload)
    except Exception:
        log.exception("vault_history.record_failed", engine=engine)

    # Refresh the full metrics snapshot the iOS companion reads (total chunks,
    # per-source composition, ingestion + memory time series, source
    # catalogue). Separate best-effort block: a snapshot miss must not affect
    # the engine-run record above, nor the engine lifecycle.
    try:
        from estormi_ingestion.shared.delivery.vault_sync import push_vault_metrics  # noqa: PLC0415

        metrics = await _build_vault_metrics()
        if metrics is not None:
            await asyncio.to_thread(push_vault_metrics, metrics)
    except Exception:
        log.exception("vault_metrics.record_failed", engine=engine)


def _build_timeseries(
    day_list: list[str],
    per_day_source: dict[str, dict[str, int]],
    sources: list[str],
) -> dict[str, Any]:
    """Shape a ``{days, sources, series}`` block for one stacked-area chart.

    ``per_day_source[day][source]`` holds the value to plot (a daily delta
    for the ingestion chart, a running total for the memory chart). Sources
    with a zero value on a given day are dropped from that day's
    ``by_source`` so the companion's tooltip stays terse.
    """
    series: list[dict[str, Any]] = []
    for day in day_list:
        row = per_day_source.get(day, {})
        by_source = {s: row[s] for s in sources if row.get(s, 0)}
        series.append({"day": day, "total": sum(by_source.values()), "by_source": by_source})
    return {"days": day_list, "sources": sources, "series": series}


async def _window_deltas(
    db, day_list: list[str], start_iso: str
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Daily chunks-added-per-source over the window + per-source window totals.

    Returns ``(deltas, window_totals)`` where ``deltas[day][source]`` is the
    count added on that calendar day. ``ingested_at`` is written as
    ``datetime('now')`` (UTC ``YYYY-MM-DD HH:MM:SS``), so ``date(ingested_at)``
    buckets cleanly by calendar day. Shared by the ingestion + memory series.
    """
    cur = await db.execute(
        "SELECT date(ingested_at) AS day, source, COUNT(*) "
        "FROM chunks WHERE date(ingested_at) >= ? GROUP BY day, source",
        (start_iso,),
    )
    deltas: dict[str, dict[str, int]] = {d: {} for d in day_list}
    window_totals: dict[str, int] = {}
    for day, source, n in await cur.fetchall():
        src = source or "unknown"
        if day in deltas:
            deltas[day][src] = deltas[day].get(src, 0) + n
            window_totals[src] = window_totals.get(src, 0) + n
    await cur.close()
    return deltas, window_totals


def _cumulative_baseline(
    day_list: list[str],
    deltas: dict[str, dict[str, int]],
    window_totals: dict[str, int],
    by_source_total: dict[str, int],
) -> tuple[list[str], dict[str, dict[str, int]]]:
    """Cumulative per-source store over the window (the "memory" view).

    Baseline per source = all-time total minus everything added inside the
    window; the daily deltas accumulate on top so the last day lands exactly on
    the all-time total. Returns ``(sources, per_day_cumulative)`` with sources
    ordered busiest-first. Shared by ``/api/timeseries`` and the vault metrics
    snapshot so the two charts stay in lock-step.
    """
    sources = sorted(by_source_total, key=lambda s: (-by_source_total[s], s))
    running = {s: by_source_total.get(s, 0) - window_totals.get(s, 0) for s in sources}
    cumulative: dict[str, dict[str, int]] = {}
    for day in day_list:
        for s in sources:
            running[s] += deltas.get(day, {}).get(s, 0)
        cumulative[day] = {s: running[s] for s in sources}
    return sources, cumulative


async def compute_chunk_timeseries(db, days: int, mode: str) -> dict[str, Any]:
    """Build a ``{days, sources, series}`` stacked-area block from ``chunks``.

    ``mode='ingestion'`` plots the daily delta (chunks added per source per
    day, sources busiest-first); ``mode='memory'`` plots the cumulative store
    (see ``_cumulative_baseline``). The single source of truth for both the
    macOS ``IngestionPulse`` chart (``/api/timeseries``) and the iOS companion's
    vault-metrics snapshot.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    day_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    deltas, window_totals = await _window_deltas(db, day_list, start.isoformat())

    if mode == "memory":
        cur = await db.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source")
        by_source_total = {(row[0] or "unknown"): row[1] for row in await cur.fetchall()}
        await cur.close()
        sources, by_day = _cumulative_baseline(day_list, deltas, window_totals, by_source_total)
    else:
        sources = sorted(window_totals, key=lambda s: (-window_totals[s], s))
        by_day = deltas

    return _build_timeseries(day_list, by_day, sources)


async def _build_vault_metrics() -> dict[str, Any] | None:
    """Assemble the companion metrics snapshot from the live DB + registry.

    Returns the dict ``vault_sync.push_vault_metrics`` persists, or ``None``
    on any failure (best-effort — never raises). This belongs in mcp-server,
    not ``vault_sync``: it reads SQLite and the connector registry, which the
    pure file-writer must not depend on.

    The snapshot carries everything the iOS Metrics page renders:

      * ``totalChunks`` / ``corpus`` / ``bySource`` — current composition;
      * ``ingestion`` — daily chunks *added* per source over the window;
      * ``memory`` — cumulative chunks per source over the window (the same
        stacked-area idiom, but the running store rather than the delta);
      * ``sources`` — the read-only catalogue (every registered connector
        with its spec metadata + live config: enabled, historic-depth pick,
        filesystem root, last watermark, chunk count).
    """
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    try:
        db = sqlite_conn()

        cur = await db.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source")
        by_source = {(row[0] or "unknown"): row[1] for row in await cur.fetchall()}
        await cur.close()
        total_chunks = sum(by_source.values())

        cur = await db.execute("SELECT corpus, COUNT(*) FROM chunks GROUP BY corpus")
        corpus = {(row[0] or "personal"): row[1] for row in await cur.fetchall()}
        await cur.close()

        # Ingestion (daily delta) + memory (cumulative store) charts — same
        # shaping as the macOS ``/api/timeseries`` route; both go through the
        # shared ``compute_chunk_timeseries`` helper so the two can't drift.
        window = _VAULT_METRICS_WINDOW_DAYS
        ingestion = await compute_chunk_timeseries(db, window, "ingestion")
        memory = await compute_chunk_timeseries(db, window, "memory")

        # Read-only source catalogue — spec metadata joined with live config.
        cur = await db.execute("SELECT source, last_fetched_at FROM ingestion_watermarks")
        watermarks = {row[0]: row[1] for row in await cur.fetchall()}
        await cur.close()
        settings = await _jobs._settings_snapshot()

        sources: list[dict[str, Any]] = []
        for spec in _jobs._registry.specs():
            name = spec.name
            enabled = (settings.get(f"source_{name}_enabled", "false") or "false") not in (
                "false",
                "",
            )
            depth = (settings.get(f"{name}_historic_depth", "") or "").lower().strip() or None
            root = (
                (settings.get(f"{name}_root", "") or "").strip() or None
                if spec.requires_root
                else None
            )
            sources.append(
                {
                    "name": name,
                    "title": spec.title,
                    "description": spec.description,
                    "chunks": by_source.get(name, 0),
                    "enabled": enabled,
                    "lastFetchedAt": watermarks.get(name),
                    "historicDepth": depth,
                    "depthWindowEnv": spec.depth_window_env,
                    "root": root,
                    "permissions": list(spec.macos_permissions),
                    "usesWatermark": spec.uses_watermark,
                    "requiresRoot": spec.requires_root,
                    "dagStage": spec.dag_stage,
                    "dagOrder": spec.dag_order,
                }
            )
        sources.sort(key=lambda s: (-s["chunks"], s["name"]))

        return {
            "version": 1,
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "totalChunks": total_chunks,
            "corpus": corpus,
            "bySource": by_source,
            "ingestion": ingestion,
            "memory": memory,
            "sources": sources,
        }
    except Exception:
        log.exception("vault_metrics.build_failed")
        return None
