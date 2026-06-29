"""Real-backend e2e: hybrid search correctness over a REAL embedded Qdrant.

Every other Python test stubs the vector store and the embedder (see the
``mock_qdrant`` / ``mock_embedder`` fixtures in ``tests/conftest.py``), so
search *ranking* — the thing that actually makes retrieval useful — is never
gated in pytest. The hermetic shell suite (``scripts/test_suite.sh``) is the
only thing that touches a real Qdrant, and it runs out-of-process.

This module closes that gap in-process: it wires ``tools`` to a real
local-mode ``AsyncQdrantClient(path=...)`` and a real file-backed SQLite,
embeds a tiny corpus with the production fastembed models (no patching of
``embed_one`` / ``sparse_embed_one``), then asserts the retrieval invariants
that mirror ``scripts/test_suite.sh`` section 3 — semantic ranking, source
filter, date-window exclusion, dedup, and the hybrid result shape.

Marked ``slow`` because it loads real embedding models; ``make test-fast``
(``-m "not slow ..."``) excludes it, ``make test-e2e`` (``-m e2e``) includes
it. The module carries a generous ``timeout`` so the FIRST real model
load/download isn't killed by the global 60 s per-test ceiling (a cold model
cache — every fresh clone — otherwise errors out red instead of skipping). If
fastembed or its models are unavailable, the module skips with a clear reason
in plain dev runs — but on the gate (``CI`` *or* ``ESTORMI_GATE`` set, the
latter exported by ``make test``/``make check``) it FAILS instead: a skipped
warmup on the gate would mean a model-download flake silently turned the only
real embeddings+Qdrant gate green.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# timeout(300): the first test to use ``real_vector_tools`` pays the full
# fastembed model load (and a download on a cold cache) inside fixture setup,
# which routinely exceeds the global 60 s per-test ceiling. The generous
# override keeps the legitimate first-load from being killed while every other
# test in the suite keeps the 60 s hung-test backstop.
pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(300)]


@pytest.fixture
async def real_vector_tools(tmp_path):
    """Wire ``tools`` to a REAL embedded Qdrant + real SQLite, then restore.

    Opts out of the ``mock_qdrant`` / ``mock_embedder`` stubs entirely by
    constructing the genuine backends here and assigning them onto the module
    globals the production code reaches through (``tools._qdrant``,
    ``tools._db``). ``ensure_collection`` builds the same dense+sparse named
    vectors and payload indexes production uses, so the search path under test
    is byte-for-byte the runtime one.

    Skips the module if fastembed (or its model cache) is unavailable locally;
    in CI the warmup failure is a hard fail (see the module docstring).
    """
    import aiosqlite

    # Real embeddings are the whole point — skip cleanly if they can't load.
    pytest.importorskip("fastembed", reason="fastembed not installed")
    try:
        from memory_core.embedder import embed_one, sparse_embed_one

        await embed_one("warmup probe")
        await sparse_embed_one("warmup probe")
    except Exception as exc:  # pragma: no cover - environment guard
        if os.environ.get("CI") or os.environ.get("ESTORMI_GATE"):
            pytest.fail(
                "fastembed model warmup failed on the gate — the real-embeddings "
                f"e2e gate must not be silently skipped here: {exc}"
            )
        pytest.skip(f"fastembed models unavailable: {exc}")

    from qdrant_client import AsyncQdrantClient

    from estormi_server.storage import search_api, tools, writers
    from estormi_server.storage.qdrant_helpers import ensure_collection
    from tests.helpers.database import apply_runtime_schema

    # Real file-backed SQLite with the full production schema.
    db = await aiosqlite.connect(str(tmp_path / "estormi.db"))
    db.row_factory = aiosqlite.Row
    await apply_runtime_schema(db)

    # Real local-mode Qdrant — no server, an embedded store under tmp_path.
    qdrant = AsyncQdrantClient(path=str(tmp_path / "qdrant"))

    saved_db = tools._db
    saved_qdrant = tools._qdrant
    saved_ready = tools._collection_ready

    tools._db = db
    tools._qdrant = qdrant
    tools._collection_ready = False

    # Build the collection with the production vector params (dense COSINE at
    # EMBED_DIM + a sparse BM25 vector + the payload indexes).
    await ensure_collection()
    tools._collection_ready = True

    try:
        # The write/read entrypoints under test — they reach the real backends
        # wired above through the late-bound ``tools.<name>`` globals.
        yield SimpleNamespace(
            ingest_chunk=writers.ingest_chunk,
            search_memory=search_api.search_memory,
        )
    finally:
        await db.close()
        await qdrant.close()
        tools._db = saved_db
        tools._qdrant = saved_qdrant
        tools._collection_ready = saved_ready


# A tiny, realistic corpus across 3 sources and 3 dates. Topics are kept
# distinct so semantic ranking has an unambiguous correct answer.
_CORPUS = [
    {
        "text": (
            "Met with Alice about the Paris product launch. Alice owns the PR "
            "agency follow-up and the demo booth checklist for the keynote."
        ),
        "source": "notes",
        "title": "Paris launch planning",
        "date": "2026-04-18T10:00:00Z",
        "content_hash": "rt-notes-paris",
        "source_id": "rt-notes-1",
    },
    {
        "text": (
            "Rendez-vous chez le dentiste, Dr. Martin, le 5 mai a 15h30. "
            "Penser a confirmer le creneau pour le detartrage."
        ),
        "source": "documents",
        "title": "Dentist appointment",
        "date": "2026-04-21T09:00:00Z",
        "content_hash": "rt-docs-dentist",
        "source_id": "rt-docs-1",
    },
    {
        "text": (
            "Refactored the search_memory hybrid retrieval: dense plus BM25 "
            "fused with reciprocal rank fusion, then blended with a recency "
            "decay score before sorting."
        ),
        "source": "code",
        "title": "Hybrid search refactor",
        "date": "2026-04-19T11:00:00Z",
        "content_hash": "rt-code-search",
        "source_id": "rt-code-1",
    },
    {
        "text": (
            "Grocery list for the weekend: tomatoes, fresh basil, mozzarella, "
            "olive oil, and a baguette for Sunday lunch."
        ),
        "source": "notes",
        "title": "Weekend groceries",
        "date": "2026-04-20T08:00:00Z",
        "content_hash": "rt-notes-groceries",
        "source_id": "rt-notes-2",
    },
    {
        "text": (
            "Booked train tickets to Lyon for the engineering offsite; the "
            "team will review the quarterly roadmap and on-call rotation."
        ),
        "source": "code",
        "title": "Lyon offsite logistics",
        "date": "2026-04-22T14:00:00Z",
        "content_hash": "rt-code-offsite",
        "source_id": "rt-code-2",
    },
]


async def _seed(store) -> None:
    for c in _CORPUS:
        res = await store.ingest_chunk(
            text=c["text"],
            source=c["source"],
            title=c["title"],
            date=c["date"],
            content_hash=c["content_hash"],
            source_id=c["source_id"],
            meta={"pii_filtered": True},
        )
        assert res["status"] == "ok", res


class TestSearchRoundtripReal:
    async def test_semantic_query_ranks_topical_chunk_first(self, real_vector_tools):
        store = real_vector_tools
        await _seed(store)

        # A query about the Paris launch with Alice — phrased differently from
        # the stored text, so a non-empty result alone is not enough: the
        # Paris note must outrank the four unrelated chunks.
        results = await store.search_memory(
            query="who is handling the Paris launch event with Alice",
            limit=5,
        )
        assert results, "expected the real vector store to return hits"
        top = results[0]
        blob = f"{top.get('title', '')} {top.get('text', '')}".lower()
        assert "paris" in blob and "alice" in blob, (
            "top hit should be the Paris launch note, got: "
            f"{top.get('title')!r} / {top.get('source')!r}"
        )

        # A clearly different topic must resolve to its own chunk, proving the
        # ranking discriminates rather than returning a fixed favourite.
        code_results = await store.search_memory(
            query="reciprocal rank fusion dense and BM25 hybrid retrieval",
            limit=5,
        )
        assert code_results
        assert code_results[0]["title"] == "Hybrid search refactor", code_results[0]

    async def test_source_filter_restricts_results(self, real_vector_tools):
        store = real_vector_tools
        await _seed(store)

        results = await store.search_memory(
            query="launch planning and roadmap",
            limit=10,
            source_filter="notes",
        )
        assert results, "source filter should still return the notes chunks"
        assert all(r["source"] == "notes" for r in results), [r["source"] for r in results]

    async def test_date_window_excludes_out_of_window_chunks(self, real_vector_tools):
        store = real_vector_tools
        await _seed(store)

        # Window covering only 2026-04-18..2026-04-19 — excludes groceries,
        # dentist, and the Lyon offsite (all dated 04-20 or later).
        windowed = await store.search_memory(
            query="planning and search work",
            limit=10,
            after="2026-04-18T00:00:00Z",
            before="2026-04-19T23:59:59Z",
        )
        assert windowed, "in-window chunks should still match"
        returned_titles = {r["title"] for r in windowed}
        assert returned_titles <= {"Paris launch planning", "Hybrid search refactor"}, (
            returned_titles
        )
        assert "Weekend groceries" not in returned_titles
        assert "Lyon offsite logistics" not in returned_titles

        # A far-future window matches nothing, mirroring the shell suite.
        empty = await store.search_memory(
            query="anything at all",
            limit=10,
            after="2099-01-01T00:00:00Z",
            before="2099-12-31T00:00:00Z",
        )
        assert empty == [], empty

    async def test_duplicate_content_hash_is_rejected(self, real_vector_tools):
        store = real_vector_tools

        first = await store.ingest_chunk(
            text="A unique decision recorded once.",
            source="notes",
            title="Dedup probe",
            date="2026-04-18T10:00:00Z",
            content_hash="rt-dedup-1",
            source_id="rt-dedup",
            meta={"pii_filtered": True},
        )
        assert first["status"] == "ok"

        second = await store.ingest_chunk(
            text="A unique decision recorded once.",
            source="notes",
            title="Dedup probe",
            date="2026-04-18T10:00:00Z",
            content_hash="rt-dedup-1",
            source_id="rt-dedup",
            meta={"pii_filtered": True},
        )
        assert second["status"] == "skipped"
        assert second["reason"] == "duplicate"
        assert second["id"] == first["id"]

    async def test_min_score_is_absolute_cosine_and_floors_unrelated(self, real_vector_tools):
        """The briefing's event correlation gates on ``min_score`` — an ABSOLUTE
        dense-cosine floor. This proves, against real fastembed output, the two
        properties hybrid RRF cannot give it: (1) ``relevance`` carries the real
        cosine, not the rank-pinned 1.0 the hybrid top hit always shows; (2) a
        query unrelated to everything stored returns NOTHING once the floor sits
        above the pool's real similarity — so a routine event invents no link.
        """
        store = real_vector_tools
        await _seed(store)

        # A genuinely-related query clears a moderate floor and surfaces its chunk.
        related = await store.search_memory(
            query="who is handling the Paris launch with Alice",
            limit=5,
            min_score=0.5,
        )
        assert related, "a genuinely related query must clear the floor"
        top = related[0]
        assert "paris" in f"{top.get('title', '')} {top.get('text', '')}".lower()
        assert all(r["relevance"] >= 0.5 for r in related), [r["relevance"] for r in related]

        # An unrelated query: read its real cosine ceiling with the floor off.
        topic = "underwater basket weaving championship qualifying rules"
        probe = await store.search_memory(query=topic, limit=5, min_score=0.0)
        assert probe, "floor=0 must still return the candidate pool"
        ceiling = max(r["relevance"] for r in probe)
        # The signature of the fix: dense-only relevance is the real cosine, NOT
        # the rank-pinned 1.0 the buggy hybrid gate read for every top hit.
        assert ceiling < 1.0, f"relevance must be absolute cosine, got {ceiling}"
        # Raising the floor above that ceiling rejects the whole unrelated pool.
        above = await store.search_memory(query=topic, limit=5, min_score=min(ceiling + 0.05, 1.0))
        assert above == [], [(r["relevance"], r["title"]) for r in above]

    async def test_result_objects_carry_hybrid_fields(self, real_vector_tools):
        store = real_vector_tools
        await _seed(store)

        results = await store.search_memory(
            query="search pipeline source filters fusion",
            limit=3,
        )
        assert results
        required = {"score", "fusion_score", "recency", "source"}
        for r in results:
            assert required <= set(r), f"missing hybrid fields: {required - set(r)}"
            assert isinstance(r["score"], float)
            assert isinstance(r["fusion_score"], float)
            assert 0.0 <= r["recency"] <= 1.0
            assert r["source"] in {"notes", "code", "documents"}
