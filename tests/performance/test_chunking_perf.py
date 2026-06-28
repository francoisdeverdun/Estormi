"""Performance benchmark: real chunker throughput.

Exercises the production sliding-window chunker from
``estormi_ingestion/shared/chunker.py``. A regression that makes ``sliding_chunks``
quadratic (or otherwise pathologically slow) on large documents would show
up here as a blown time budget.
"""

from __future__ import annotations

import time

import pytest

from estormi_ingestion.shared.chunker import sliding_chunks

pytestmark = pytest.mark.performance

# Generous ceiling — these run on shared CI runners. The real chunker is
# linear, so even a large document finishes in a few milliseconds; a second
# of headroom only trips on a genuine algorithmic regression.
MAX_CHUNKING_SECONDS = 2.0


def test_sliding_chunks_large_document_throughput():
    """Chunking a ~1M-char document with the real chunker stays fast and correct."""
    text = "word " * 200_000  # ~1M chars

    start = time.perf_counter()
    chunks = sliding_chunks(text, size=800, overlap=100)
    elapsed = time.perf_counter() - start

    # Correctness of the benchmarked operation: a 1M-char document split into
    # 800-char windows stepping by 700 must yield well over a thousand chunks,
    # and every chunk must respect the size bound.
    assert len(chunks) > 1000
    assert all(len(c) <= 800 for c in chunks)
    assert elapsed < MAX_CHUNKING_SECONDS, (
        f"sliding_chunks took {elapsed:.3f}s for a 1M-char document (limit {MAX_CHUNKING_SECONDS}s)"
    )


def test_sliding_chunks_many_small_documents_throughput():
    """Chunking 5 000 small documents in a loop stays within budget."""
    docs = [f"Memory fragment number {i} about a durable decision. " * 20 for i in range(5_000)]

    start = time.perf_counter()
    total = 0
    for doc in docs:
        total += len(sliding_chunks(doc, size=800, overlap=100))
    elapsed = time.perf_counter() - start

    assert total >= len(docs), "every non-empty document must produce at least one chunk"
    assert elapsed < MAX_CHUNKING_SECONDS, (
        f"chunking 5000 small documents took {elapsed:.3f}s (limit {MAX_CHUNKING_SECONDS}s)"
    )
