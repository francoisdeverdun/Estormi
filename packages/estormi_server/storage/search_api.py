"""Query-time hybrid search.

Owns :func:`search_memory` and its small private helpers (date parsing,
recency decay). The shared Qdrant client, the embedding functions and the
SQLite connection all live on :mod:`tools`; this module reaches them via
attribute access (``tools.embed_one``, ``tools.sqlite_conn``, …) so the test
suite's ``patch("estormi_server.storage.tools.<name>", …)`` hooks keep working transparently.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from math import exp

import structlog
from qdrant_client.models import (
    DatetimeRange,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    Prefetch,
    SparseVector,
)

from estormi_server.storage.qdrant_helpers import _search_result_text
from memory_core.audit import log_tool_call
from memory_core.sanitizer import sanitize_query
from memory_core.timeparse import local_day_window, parse_iso

log = structlog.get_logger(__name__)

RECENCY_WEIGHT = float(os.getenv("RECENCY_WEIGHT", "0.3"))
RECENCY_HALF_LIFE_DAYS = max(1e-6, float(os.getenv("RECENCY_HALF_LIFE_DAYS", "180")))


# Local alias for the shared parser in memory_core — kept under the historical
# name the search helpers (and their tests) use.
_parse_date_ts = parse_iso

# A bare calendar date (no time component) — anchored on the LOCAL day in
# fetch_around. Strict full-match so a stray "2026-06-14Z" (no T/colon but a
# real instant) is treated as the timestamp it is, not misread as a bare day.
_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _recency_score(date_ts: str | None, now: datetime) -> float:
    """Symmetric exponential decay in [0, 1]; 1.0 at now, 0.5 at one half-life of
    distance in *either* direction.

    Distance from now is taken as ``abs(age)`` so a future-dated chunk (an
    upcoming calendar event / reminder) decays just like an equally-distant past
    one. Without the ``abs`` a future ``date_ts`` clamped to age 0 scored a flat
    1.0 — tying with "now" and out-ranking every genuinely-recent past memory.
    """
    dt = _parse_date_ts(date_ts)
    if dt is None:
        return 0.0
    age_days = abs((now - dt).total_seconds()) / 86400.0
    return exp(-age_days * 0.6931471805599453 / RECENCY_HALF_LIFE_DAYS)


async def search_memory(
    query: str,
    limit: int = 10,
    source_filter: str | None = None,
    after: str | None = None,
    before: str | None = None,
    group_type: str | None = None,
    chat_kind: str | None = None,
    pending_reply: bool | None = None,
    sources: list | None = None,
    corpus: str | None = None,
    min_score: float | None = None,
) -> list[dict]:
    """Hybrid semantic search — dense + BM25 fused via RRF, optional date window.

    ``corpus`` scopes to ``personal`` or ``world`` chunks; left unset, both are
    searched. A personal query passes ``corpus='personal'`` so world news never
    crowds out the user's own memory.

    ``min_score`` switches to an *absolute-relatedness* retrieval: a dense-only
    cosine query with ``score_threshold=min_score`` instead of hybrid RRF. RRF
    scores are rank-based — the top hit is rank 0 → ``relevance`` 1.0 even for a
    query unrelated to anything stored — so they answer "best of these", never
    "is this actually related". Real fastembed cosine does: a genuine match
    scores ~0.85/0.63, unrelated same-language text clusters at ~0.43–0.59, so a
    ~0.6 floor cleanly separates them. The briefing's event correlation passes
    ``min_score`` so it links only chunks that truly relate to the event, not
    merely the closest of an unrelated pool. In this mode ``relevance`` and
    ``fusion_score`` carry the absolute cosine, not a min-max-normalised rank.
    """
    # Late-binding through ``tools`` so the test suite's
    # ``patch("estormi_server.storage.tools.embed_one", ...)`` and ``patch("estormi_server.storage.tools._client", ...)``
    # hooks affect this caller too.
    from estormi_server.storage import tools  # noqa: PLC0415

    t0 = time.time()
    clean_query = sanitize_query(query)

    # Lazy collection setup — see _ensure_collection_ready. If startup
    # failed to create the schema (Qdrant lock), the first search after
    # boot would otherwise hit a non-existent collection.
    await tools._ensure_collection_ready()

    dense_vec = await tools.embed_one(clean_query)

    must: list = []

    # Multi-source OR filter takes priority over single source_filter
    if sources and len(sources) > 0:
        must.append(
            Filter(
                should=[FieldCondition(key="source", match=MatchValue(value=s)) for s in sources]
            )
        )
    elif source_filter:
        must.append(FieldCondition(key="source", match=MatchValue(value=source_filter)))

    gte = _parse_date_ts(after)
    lte = _parse_date_ts(before)
    # ``after``/``before`` are optional, but a non-empty unparseable value
    # ("yesterday", "2026-13-99") is a client error — surface it like
    # ``fetch_around`` does for ``date`` rather than silently dropping the
    # window and returning unbounded results that look like a success.
    for _name, _raw, _parsed in (("after", after, gte), ("before", before, lte)):
        if _raw and _parsed is None:
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(
                status_code=400, detail=f"Unparseable {_name!r} parameter: {_raw!r}"
            )
    if gte or lte:
        must.append(
            FieldCondition(
                key="date_ts",
                range=DatetimeRange(gte=gte, lte=lte),
            )
        )
    if group_type:
        must.append(FieldCondition(key="group_type", match=MatchValue(value=group_type)))
    if chat_kind:
        must.append(FieldCondition(key="chat_kind", match=MatchValue(value=chat_kind)))
    # Only True is supported: tools.py persists the `pending_reply` payload field
    # only when truthy, so a `pending_reply=False` filter would match zero chunks.
    if pending_reply:
        must.append(FieldCondition(key="pending_reply", match=MatchValue(value=True)))
    if corpus:
        must.append(FieldCondition(key="corpus", match=MatchValue(value=corpus)))

    q_filter = Filter(must=must) if must else None

    top_k = min(max(1, limit), 100)
    prefetch_pool = max(top_k * 4, 40)

    # Absolute-relatedness mode (see docstring): dense-only cosine query gated by
    # ``score_threshold`` so the candidate pool is pre-filtered to chunks that
    # genuinely relate to the query. Sparse/BM25 is deliberately dropped here —
    # a lexical-only match (shared word, low cosine) is exactly the spurious
    # link this mode exists to reject.
    dense_only = min_score is not None
    if dense_only:
        results = await tools._client().query_points(
            collection_name=tools.COLLECTION,
            query=dense_vec,
            using=tools.DENSE_VECTOR_NAME,
            query_filter=q_filter,
            score_threshold=float(min_score),
            limit=prefetch_pool,
            with_payload=True,
        )
    else:
        # Sparse/BM25 is only embedded for hybrid mode — dense-only relatedness
        # never uses it, so we skip the extra embed on every correlation call.
        sparse_vec = await tools.sparse_embed_one(clean_query)
        results = await tools._client().query_points(
            collection_name=tools.COLLECTION,
            prefetch=[
                Prefetch(
                    query=dense_vec,
                    using=tools.DENSE_VECTOR_NAME,
                    limit=prefetch_pool,
                    filter=q_filter,
                ),
                Prefetch(
                    query=SparseVector(
                        indices=sparse_vec["indices"],
                        values=sparse_vec["values"],
                    ),
                    using=tools.SPARSE_VECTOR_NAME,
                    limit=prefetch_pool,
                    filter=q_filter,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            # Redundant with the per-prefetch filters above (both candidate pools are
            # already scoped, so RRF fuses only matching rows) — kept as a defensive
            # belt-and-braces guard on the fused result.
            query_filter=q_filter,
            # Fetch the full fused candidate pool, not just the caller's ``limit``,
            # so the min-max normalisation below runs over a population that does
            # not change with ``limit``. Normalising over only the top-``limit`` rows
            # made the head order depend on ``limit`` (a low-fusion, very-old outlier
            # entering at a larger limit widened the span and could flip two
            # near-tied results). We blend + sort over the pool, then slice to top_k.
            limit=prefetch_pool,
            with_payload=True,
        )

    now = datetime.now(timezone.utc)
    # Qdrant's RRF returns scores like 1/(60+rank) — typical magnitudes are
    # ~0.02 even for the top hit, whereas recency lives in [0, 1]. Blending
    # them directly would let recency dominate by ~50× and turn the result
    # set into "newest first regardless of relevance". Normalise the fusion
    # score to [0, 1] via min-max across the full fused pool (not the top-``limit``
    # slice — see the query above) before blending.
    raw_points = list(results.points)
    raw_scores = [r.score or 0.0 for r in raw_points]
    if raw_scores:
        lo, hi = min(raw_scores), max(raw_scores)
        span = hi - lo
    else:
        lo, span = 0.0, 0.0

    def _normalise(score: float) -> float:
        # Degenerate single-valued pool (every fused score equal, span 0): the
        # min-max "best of this pool" position is undefined — there is no spread
        # to rank against. Return a neutral 0.5 rather than stamping a fabricated
        # 1.0 ("perfect relevance") on every chunk; recency then breaks the tie.
        if span <= 0.0:
            return 0.5 if score > 0.0 else 0.0
        return (score - lo) / span

    chunks: list[dict] = []
    for r in raw_points:
        payload = r.payload or {}
        base = float(r.score or 0.0)
        # Dense-only mode: ``base`` is already the absolute cosine, which IS the
        # relevance signal — min-max-normalising it would destroy the absolute
        # meaning the caller asked for. Hybrid mode: normalise the rank-based RRF
        # score to [0, 1] so it blends sanely with recency.
        norm_base = base if dense_only else _normalise(base)
        recency = _recency_score(payload.get("date_ts") or payload.get("date"), now)
        # Dense-only mode is an *absolute-relatedness* retrieval (see docstring):
        # the caller — the briefing's event correlation, which slices the top N —
        # wants results ordered by pure relatedness, not recency. Blending recency
        # here would let a barely-related-but-recent chunk outrank a more-related
        # older one within the min_score floor and steal a correlation slot. So we
        # rank dense-only by cosine alone; only hybrid mode co-ranks by recency.
        if dense_only:
            final_score = norm_base
        else:
            final_score = (1.0 - RECENCY_WEIGHT) * norm_base + RECENCY_WEIGHT * recency
        chunks.append(
            {
                "id": str(r.id),
                "score": round(final_score, 4),
                "fusion_score": round(base, 4),
                # Un-blended relevance in [0, 1], before recency mixing. Hybrid
                # mode: min-max-normalised RRF score — a *relative* "best of this
                # pool" rank, NOT absolute relatedness (the top hit is always
                # 1.0). Dense-only mode (``min_score`` set): the absolute cosine,
                # which a caller CAN threshold for "is this actually related".
                "relevance": round(norm_base, 4),
                "recency": round(recency, 4),
                "text": _search_result_text(payload),
                "source": payload.get("source"),
                "source_id": payload.get("source_id"),
                "title": payload.get("title"),
                "date": payload.get("date"),
                "url": payload.get("url"),
                "group_type": payload.get("group_type"),
                "chat_kind": payload.get("chat_kind"),
                "pending_reply": payload.get("pending_reply"),
                "chat_id_raw": payload.get("chat_id_raw"),
                "corpus": payload.get("corpus"),
                "event_type": payload.get("event_type"),
                "event_status": payload.get("event_status"),
                "working_location": payload.get("working_location"),
            }
        )

    chunks.sort(key=lambda c: c["score"], reverse=True)
    chunks = chunks[:top_k]

    # ``sub``/``email`` resolve to "unknown" by design: Estormi authenticates
    # with a single static bearer token (see ``server.security``), not a
    # per-user JWT, so there is no caller identity to record. The audit row
    # intentionally carries "unknown" rather than a fabricated identity.
    log_tool_call(
        token_sub="unknown",
        token_email="unknown",
        tool_name="search_memory",
        query=clean_query,
        # Log the ids actually returned, in returned (post-recency-sort, sliced)
        # order — not the pre-sort fusion order, which the caller never sees.
        result_ids=[c["id"] for c in chunks],
        duration_ms=(time.time() - t0) * 1000,
    )
    return chunks


async def fetch_around(
    date: str,
    window_days: int = 1,
    sources: list | None = None,
    corpus: str | None = None,
    limit: int = 200,
    forward_days: int | None = None,
) -> list[dict]:
    """Time-window retrieval across every source — the correlation primitive.

    Returns each chunk whose ``[date_ts, end_date_ts]`` interval overlaps the
    window centred on ``date`` (± ``window_days`` days), newest first, across
    all sources. Things about the same real-world event (a mail, the calendar
    entry, a reminder, a chat) cluster in time, so handing the model one window
    lets it weave the thread without any pre-computed link table.

    No embedding step: this is a pure ``date_ts``-indexed SQLite range scan,
    with chunk text hydrated from the Qdrant payload. ``corpus`` scopes to
    ``personal`` / ``world``; ``sources`` restricts to a subset.
    ``forward_days`` optionally caps the look-ahead on its own — pass 0 to keep
    the window from crossing into tomorrow (look-back stays ``window_days``).
    """
    from estormi_server.storage import tools  # noqa: PLC0415

    center = _parse_date_ts(date)
    if center is None:
        # ``date`` is required by the inputSchema / REST body; an unparseable
        # value (typo, "yesterday", "2026-13-99") is a client error, not a
        # genuinely-empty window. Surface it so the caller can correct it
        # instead of silently returning [] (indistinguishable from no hits).
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(status_code=400, detail=f"Unparseable 'date' parameter: {date!r}")
    # Clamp both bounds to the schema/REST contract (inputSchema maximum:90,
    # FetchAroundBody le=90); the MCP path has no Pydantic validation, so an
    # out-of-range window_days=100000 would otherwise be honored. 90 accommodates
    # the briefing's ~2-month correlation forward horizon.
    window_days = min(max(0, int(window_days)), 90)
    # The look-ahead defaults to a symmetric window; ``forward_days`` bounds it
    # independently of the look-back. forward_days=0 stops the window at the end
    # of the centre day, so "today's" context never pulls a next-day calendar
    # entry (see day_context._fetch_day_context_chunks). None preserves the
    # original symmetric behaviour exactly.
    fwd = window_days if forward_days is None else min(max(0, int(forward_days)), 90)
    # A *bare* date (no time component) names a LOCAL calendar day, so anchor the
    # window on that local day's edges (ESTORMI_LOCAL_TZ — the same resolution the
    # Briefing's day bucketing uses) rather than on UTC midnight. For a non-UTC
    # user, UTC anchoring made a forward_days=0 window leak tomorrow's early
    # morning (east of UTC) or drop today's evening (west) — exactly the
    # "briefing mixes current & next day" report. An explicit timestamp keeps the
    # original instant-anchored window: it already names a precise moment.
    try:
        if _BARE_DATE_RE.match(date.strip()):
            lo, hi = local_day_window(center.date(), window_days, fwd)
        else:
            lo = (center - timedelta(days=window_days)).isoformat()
            # The window is day-granular and inclusive of its LAST day: "+ fwd"
            # alone would end it at the final day's 00:00 — fwd=0 would become a
            # single instant (every chunk of the day itself missed) and every
            # window would silently lose its last day.
            hi = (center + timedelta(days=fwd + 1)).isoformat()
    except OverflowError:
        # An extreme-year centre (e.g. 9999-12-31 or 0001-01-01) overflows
        # date arithmetic. That's a malformed client date, not an internal
        # fault, so surface it as a 400 like the unparseable case above rather
        # than letting OverflowError bubble to a generic 500.
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(
            status_code=400, detail=f"'date' parameter out of representable range: {date!r}"
        ) from None

    # Overlap test on real instants, not raw ISO strings: a chunk touches the
    # window when it starts on/before the window end and ends on/after the
    # window start. Stored ``date_ts`` values carry their source offset (e.g.
    # Google Calendar feeds ``+02:00``, most others ``+00:00``), so a plain
    # string ``<=``/``>=`` would compare wall-clock text and mis-order chunks
    # across timezones/DST. ``datetime()`` normalises every offset to UTC before
    # comparing (verified: ``datetime('…+02:00')`` → the 08:00Z instant). The
    # ``datetime(date_ts)`` expression is matched by the expression index
    # ``chunks_date_ts_utc_idx`` (see schema_migrations), so this is an indexed
    # range scan, not the full table scan a bare-column index would force here.
    # ``end_date_ts`` is null for point-in-time chunks, so COALESCE to date_ts.
    where = [
        "date_ts IS NOT NULL",
        "datetime(date_ts) <= datetime(?)",
        "datetime(COALESCE(end_date_ts, date_ts)) >= datetime(?)",
    ]
    args: list = [hi, lo]
    if corpus:
        where.append("corpus = ?")
        args.append(corpus)
    if sources:
        placeholders = ",".join("?" for _ in sources)
        where.append(f"source IN ({placeholders})")
        args.extend(sources)
    args.append(min(max(1, int(limit)), 500))

    db = tools.sqlite_conn()
    # ORDER BY datetime(date_ts), not raw date_ts: the same heterogeneous offsets
    # the WHERE normalises (gcal feeds ``+02:00``, others ``Z``) would otherwise
    # sort lexically — a ``…+02:00`` string sorts after an earlier-instant ``…Z``
    # one — so on a dense window that hits LIMIT, truly-newer chunks could fall
    # below the cut and break "newest first". datetime() compares real instants,
    # and matches chunks_date_ts_utc_idx so the sort is index-ordered, not a
    # filesort.
    cursor = await db.execute(
        "SELECT id, source, source_id, title, date, date_ts, end_date_ts, "
        "group_type, chat_kind, corpus, event_type, event_status, working_location FROM chunks "
        f"WHERE {' AND '.join(where)} ORDER BY datetime(date_ts) DESC LIMIT ?",
        args,
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if not rows:
        return []

    # Text lives only in the Qdrant payload (the chunk id is the point id).
    ids = [r["id"] for r in rows]
    text_by_id: dict[str, str] = {}
    try:
        points = await tools._client().retrieve(
            collection_name=tools.COLLECTION,
            ids=ids,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            text_by_id[str(p.id)] = _search_result_text(p.payload or {})
    except Exception as exc:
        # Don't swallow this: SQLite has the rows but the text lives only in
        # Qdrant, so a failed hydrate would return chunks with empty ``text``.
        # The briefing then correlates over blank bodies and silently drops the
        # window — strictly worse than the empty-result case the date branch
        # above already refuses. Surface it so the caller fails loudly instead.
        from fastapi import HTTPException  # noqa: PLC0415

        log.warning("fetch_around.qdrant_retrieve_failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Memory store unavailable: could not hydrate chunk text from Qdrant.",
        ) from exc

    def _row(r, txt: str) -> dict:
        return {
            "id": r["id"],
            "source": r["source"],
            "source_id": r["source_id"],
            "title": r["title"],
            "date": r["date"],
            "date_ts": r["date_ts"],
            "end_date_ts": r["end_date_ts"],
            "group_type": r["group_type"],
            "chat_kind": r["chat_kind"],
            "corpus": r["corpus"],
            "event_type": r["event_type"],
            "event_status": r["event_status"],
            "working_location": r["working_location"],
            "text": txt,
        }

    # A *partial* Qdrant miss — some ids hydrate, an orphan SQLite row (vector
    # lost) does not — slips past the total-failure 503 branch above and would
    # seed the correlation window with a blank-bodied chunk. When hydration
    # resolved at least one point, drop the rows that came back empty and log
    # the SQLite/Qdrant divergence by id so it stays observable. (A wholly-empty
    # result is the total-miss edge; leave those rows as-is.)
    if text_by_id:
        out, dropped = [], []
        for r in rows:
            txt = text_by_id.get(r["id"], "").strip()
            if txt:
                out.append(_row(r, txt))
            else:
                dropped.append(r["id"])
        if dropped:
            log.warning("fetch_around.empty_text_dropped", count=len(dropped), ids=dropped)
        return out

    return [_row(r, text_by_id.get(r["id"], "")) for r in rows]
