#!/usr/bin/env python3
"""Ingest external "world" sources (YouTube transcripts + RSS articles).

This is a daily ingestion pipeline stage — the *Ingestion* engine's counterpart to
the per-source connectors under ``estormi_ingestion/<stage>/``. It fetches the same
sources the Briefing page configures (``knowledge_sources.yaml``) and stores
them as raw ``world``-corpus chunks via ``POST /ingest_chunk`` (source
``knowledge``, which maps to ``corpus=world`` server-side via ``WORLD_SOURCES``).

Unlike the Briefing engine it does **no** LLM work: it only fetches, chunks,
PII-filters and ingests. The Briefing engine then reads these ``world`` chunks
back from the DB at composition time instead of fetching transcripts itself.

Run standalone:
    python estormi_ingestion/knowledge/ingest_world.py

Or as a pipeline stage:
    python -m connectors run knowledge
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

# Sibling fetch helpers. knowledge_fetch only pulls stdlib at import time
# (feedparser / httpx are lazy inside its functions), so this stays cheap even
# when the briefing engine imports this module just for ``source_key``.
from estormi_ingestion.knowledge.knowledge_fetch import (
    cleanup_tmp_dir,
    download_transcript,
    fetch_recent_videos,
    fetch_rss_source,
)

# Shared ingestion utilities.
from estormi_ingestion.shared.chunker import sliding_chunks
from estormi_ingestion.shared.config import mcp_url
from estormi_ingestion.shared.emit import content_base_hash, post_chunks
from estormi_ingestion.shared.paths import estormi_data_dir
from estormi_ingestion.shared.watermark import get_watermark, set_watermark
from memory_core.pii_filter import filter_pii

DATA_DIR = Path(estormi_data_dir())
MCP_SERVER_URL = mcp_url()

# Stable connector/source name. Matches the connector spec, the pipeline stage, and
# the ``source`` column every chunk lands under — so the Sources catalogue
# (which counts chunks and reads the watermark by ``spec.name``) sees it.
# ``SOURCE`` is in WORLD_SOURCES (writers.py), so chunks derive ``corpus=world``.
SOURCE = "knowledge"
WATERMARK_KEY = SOURCE

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150

log = structlog.get_logger()


# ── Source config ──────────────────────────────────────────────────────────────

# ``id`` is intentionally NOT required: the UI saves sources without one, so we
# derive a stable id from the label (see ``source_key``). The Briefing engine
# uses the same derivation, keeping the world-chunk → source mapping consistent.
_RSS_REQUIRED = {"label", "type", "urls"}
_YT_REQUIRED = {"label", "type", "url", "axis", "mode", "subtitle_langs"}
_SUPPORTED_TYPES = {"youtube_channel", "rss"}


def _config_path() -> Path:
    """Resolve the sources YAML the same way the Briefing engine does: the
    user data-dir copy the Briefing page edits. No sources ship by default."""
    return DATA_DIR / "knowledge_sources.yaml"


def source_key(source: dict) -> str:
    """Stable per-source key used as the ``source_id`` prefix.

    Lets the Briefing engine map a stored ``world`` chunk back to its source
    (and thus its ``axis`` / ``mode`` / ``pre_prompt``) by reloading the same
    config — ``meta`` is not persisted on chunks, so the key has to ride in a
    field that is (``source_id``).
    """
    raw = str(source.get("id") or source.get("label") or "source")
    return raw.lower().replace(" ", "_").replace("-", "_")[:48]


def validate_sources(sources: list[dict], *, strict: bool) -> list[dict]:
    """Validate + default-fill knowledge sources. The single source of truth for
    the source schema, shared by both front doors so they cannot drift apart.

    ``strict=True`` (the Briefing engine's ``run_briefing.load_sources``) raises
    ``ValueError`` on the first bad source so a misconfigured roster fails the run
    loudly. ``strict=False`` (the ``knowledge`` ingestion stage) warns and skips
    the offending source so one typo can't abort the whole world pull.
    """
    valid: list[dict] = []
    for s in sources:
        # Operate on a shallow copy so the returned list owns its dicts and the
        # caller's input dicts are never mutated (setdefault / id assignment).
        s = dict(s)
        src_type = s.get("type", "")
        if src_type not in _SUPPORTED_TYPES:
            if strict:
                raise ValueError(f"Source {s.get('id', '?')} has unsupported type: {src_type!r}")
            log.warning("Skipping source %s: unsupported type %r", s.get("id", "?"), src_type)
            continue
        if src_type == "rss":
            missing = _RSS_REQUIRED - s.keys()
            if missing:
                if strict:
                    raise ValueError(f"Source {s.get('label', '?')} missing fields: {missing}")
                log.warning("Skipping RSS source %s: missing fields", s.get("label", "?"))
                continue
            s.setdefault("axis", "news")
            s.setdefault("mode", "news")
        else:
            missing = _YT_REQUIRED - s.keys()
            if missing:
                if strict:
                    raise ValueError(f"Source {s.get('label', '?')} missing fields: {missing}")
                log.warning("Skipping YouTube source %s: missing fields", s.get("label", "?"))
                continue
            if s["mode"] not in ("news", "analysis", "opinion"):
                if strict:
                    raise ValueError(
                        f"Source {s.get('label', '?')} has invalid mode: {s['mode']!r}"
                    )
                log.warning(
                    "Skipping YouTube source %s: invalid mode %r", s.get("label", "?"), s["mode"]
                )
                continue
        # Optional flag: the source promotes its own product (a company
        # channel, a sponsored feed). Normalised to a real bool so the
        # summarisation prompts can rely on it; the briefing then frames the
        # content as commercial discourse instead of restating it first-degree.
        s["promotional"] = bool(s.get("promotional", False))
        # Derive a stable id from the label when the config omits one.
        if not s.get("id"):
            s["id"] = source_key(s)
        valid.append(s)
    return valid


def load_sources(config_path: Path) -> list[dict]:
    """Load + validate sources, filling the same defaults as the Briefing engine."""
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return validate_sources(data.get("sources", []) or [], strict=False)


# ── Fetch → world items ─────────────────────────────────────────────────────────


def _collect_world_items(source: dict, *, lookback_days: int, today: str) -> list[dict]:
    """Fetch one source into raw world items: ``{source_id, title, text, date}``."""
    key = source_key(source)
    items: list[dict] = []

    if source["type"] == "rss":
        # Re-fetch a window wide enough to cover any missed daily runs.
        source = {
            **source,
            "window_hours": max(int(source.get("window_hours", 24)), lookback_days * 24),
        }
        articles = fetch_rss_source(source)
        log.info("%s: %d RSS articles", key, len(articles))
        for art in articles:
            art_url = (art.get("url") or "").strip()
            art_id = (
                art_url
                or hashlib.sha256(f"{key}|{art.get('title', '')}".encode("utf-8")).hexdigest()[:24]
            )
            summary = (art.get("summary") or "").strip()
            items.append(
                {
                    "source_id": f"news::{key}::{art_id}",
                    "title": art.get("title") or source["label"],
                    "text": f"{art.get('title', '')}\n{summary}".strip(),
                    "date": art.get("published") or today,
                }
            )
        return items

    try:
        videos = fetch_recent_videos(source, lookback_days=lookback_days)
    except FileNotFoundError as exc:
        # yt-dlp missing from the bundled Python — skip rather than fail the DAG.
        log.warning("Skipping %s: %s", key, exc)
        return items
    log.info("%s: %d videos (last %dd)", key, len(videos), lookback_days)

    for video in videos:
        # Per-video guard: one broken video (yt-dlp crash, disk error, …) must
        # cost only itself — never the source's already-collected items.
        try:
            transcript = download_transcript(video["id"], source["subtitle_langs"])
        except Exception:
            log.exception("%s: transcript fetch failed for video %s", key, video.get("id"))
            continue
        if not transcript:
            continue
        items.append(
            {
                "source_id": f"news::{key}::yt-{video['id']}",
                "title": video.get("title") or source["label"],
                "text": transcript,
                "date": video.get("upload_date") or today,
            }
        )
    return items


# ── Ingest ──────────────────────────────────────────────────────────────────────


def _ingest_items(items: list[dict]) -> tuple[int, int, int]:
    """POST each item's chunks. Returns ``(chunks_ok, chunks_skipped, chunks_failed)``."""
    headers = {
        "Content-Type": "application/json",
        # First-party origin marker. These root-mounted endpoints aren't behind
        # the CSRF gate (which only covers /api/* — see
        # estormi_server/server/security.py); the header is harmless defense-in-depth.
        "X-Estormi-Origin": "estormi-knowledge",
    }
    ok = skipped = failed = 0
    for item in items:
        text = filter_pii((item.get("text") or "").strip())
        if not text:
            continue
        source_id = item["source_id"]
        title = item.get("title") or source_id
        date = item.get("date") or ""
        base = content_base_hash(source_id, text)

        def _log(idx: int, status: str, _sid: str = source_id) -> None:
            if status.startswith("ERROR"):
                log.warning("chunk ingest failed (%s): %s", _sid, status.removeprefix("ERROR "))

        item_ok, item_skipped, item_failed = post_chunks(
            SOURCE,  # in WORLD_SOURCES → corpus=world server-side
            source_id,
            sliding_chunks(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP),
            mcp_url=MCP_SERVER_URL,
            title=title,
            date=date,
            meta={"pii_filtered": True},
            base_hash=base,
            headers=headers,
            on_result=_log,
        )
        ok += item_ok
        skipped += item_skipped
        failed += item_failed
    return ok, skipped, failed


# ── Main ─────────────────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[world] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    config_path = _config_path()
    if not config_path.exists():
        log.warning(
            "No knowledge sources configured (missing %s). Add sources in the Briefing page.",
            config_path,
        )
        return 0
    sources = load_sources(config_path)
    if not sources:
        log.info("No valid world sources — nothing to ingest.")
        return 0

    today = datetime.now(timezone.utc).date().isoformat()
    run_started = datetime.now(timezone.utc)

    # First-run history window (days). Set by the Manage modal's depth picker
    # via ``apply_ingest_env_overrides`` (KNOWLEDGE_DAYS_WINDOW); defaults to a
    # week so a fresh install doesn't pull months of feeds on day one.
    window_days = max(1, int(os.getenv("KNOWLEDGE_DAYS_WINDOW", "7")))

    # First run (no watermark) reaches back the full window. Subsequent runs
    # only need to catch up the gap since the last success, but never more than
    # the configured window. Best-effort — the watermark is advisory here.
    last_ts, _ = asyncio.run(get_watermark(WATERMARK_KEY))
    lookback_days = window_days
    if last_ts:
        try:
            last = datetime.fromisoformat(last_ts)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            gap = (run_started - last).days
            lookback_days = max(1, min(gap + 1, window_days))
        except ValueError:
            pass

    total_ok = total_skipped = total_failed = 0
    try:
        for source in sources:
            try:
                items = _collect_world_items(source, lookback_days=lookback_days, today=today)
                ok, skipped, failed = _ingest_items(items)
                total_ok += ok
                total_skipped += skipped
                total_failed += failed
            except Exception:
                log.exception("source %s failed", source_key(source))
                total_failed += 1
    finally:
        # Always reclaim the per-run scratch tree, even if the loop is
        # interrupted (e.g. SIGTERM from the pipeline launcher) — otherwise
        # estormi_kb_* dirs accumulate in $TMPDIR.
        cleanup_tmp_dir()
    log.info(
        "[world] Done — %d source(s), %d chunk(s) ingested, %d skipped, %d failed.",
        len(sources),
        total_ok,
        total_skipped,
        total_failed,
    )
    if total_failed and not total_ok:
        # Everything failed — surface a non-zero exit so the pipeline marks the
        # stage failed and the watermark is left untouched for a retry.
        return 1
    # Advance the watermark only when the run had no failures, so a transient
    # outage doesn't skip a day's catch-up window.
    if not total_failed:
        asyncio.run(set_watermark(WATERMARK_KEY, run_started.isoformat()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
