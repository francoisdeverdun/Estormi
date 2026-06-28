"""Embeddings:
- Dense: fastembed TextEmbedding — nomic-embed-text-v1.5 (768d, ONNX, in-process)
- Sparse: fastembed BM25 (lexical, in-process)
"""

import asyncio
import os

from fastembed import SparseTextEmbedding, TextEmbedding

EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

SPARSE_MODEL = os.getenv("SPARSE_MODEL", "Qdrant/bm25")


def _model_cache_dir() -> str:
    """Persistent fastembed model cache.

    fastembed's default cache lives under ``$TMPDIR/fastembed_cache`` — and
    macOS periodically purges /var/folders temp files, which left a snapshot
    directory whose ``model.onnx`` was gone: every ingest then 500'd until a
    manual re-download. Pin the cache next to the other model weights in the
    app data dir (``FASTEMBED_CACHE_PATH`` still overrides for tests/dev).
    """
    pinned = os.getenv("FASTEMBED_CACHE_PATH", "").strip()
    if pinned:
        return pinned
    from memory_core import settings  # noqa: PLC0415 — avoid import cycle at module load

    path = os.path.join(settings.resolve_data_dir(), "models", "fastembed_cache")
    os.makedirs(path, exist_ok=True)
    return path


_dense_model: TextEmbedding | None = None
_dense_lock = asyncio.Lock()
_dense_dim_checked: bool = False

_bm25_model: SparseTextEmbedding | None = None
_bm25_lock = asyncio.Lock()


async def _dense() -> TextEmbedding:
    global _dense_model
    async with _dense_lock:
        if _dense_model is None:
            _dense_model = await asyncio.to_thread(
                TextEmbedding, model_name=EMBED_MODEL, cache_dir=_model_cache_dir()
            )
    return _dense_model


def _verify_dense_dim(sample_vector) -> None:
    """Cheap once-per-process check that the loaded model emits ``EMBED_DIM``.

    Without this, a misconfigured ``EMBED_MODEL`` / ``EMBED_DIM`` mismatch
    silently produces vectors that Qdrant rejects on upsert with an opaque
    "wrong dimension" — or worse, builds a collection at the wrong size and
    poisons every subsequent ingest. Fail loudly at first embedding.

    Skips the check when ``sample_vector`` is too short to plausibly come
    from a real embedding model (``<16`` dims). The test suite mocks the
    embedder with toy vectors and would otherwise trip this guard.
    """
    global _dense_dim_checked
    if _dense_dim_checked:
        return
    actual = len(list(sample_vector))
    if actual < 16:
        # Mocked / toy embedder — not a real model. Don't false-trip, and do
        # NOT latch: a real model embedding later in the same process must still
        # be verified (a toy vector seen first would otherwise disable the check
        # for the rest of the run).
        return
    if actual != EMBED_DIM:
        raise RuntimeError(
            f"EMBED_DIM={EMBED_DIM} but model {EMBED_MODEL} emitted "
            f"{actual}-dim vectors. Set EMBED_DIM={actual} or change EMBED_MODEL."
        )
    _dense_dim_checked = True


async def embed(texts: list[str]) -> list[list[float]]:
    """Dense embedding for a batch of texts."""
    if not texts:
        return []
    model = await _dense()
    results = await asyncio.to_thread(lambda: list(model.embed(texts)))
    if results:
        _verify_dense_dim(results[0])
    return [list(map(float, v)) for v in results]


async def embed_one(text: str) -> list[float]:
    results = await embed([text])
    return results[0]


async def _bm25() -> SparseTextEmbedding:
    """Lazy-load the BM25 model on first use — cached under the persistent
    model cache (see :func:`_model_cache_dir`)."""
    global _bm25_model
    async with _bm25_lock:
        if _bm25_model is None:
            _bm25_model = await asyncio.to_thread(
                SparseTextEmbedding, model_name=SPARSE_MODEL, cache_dir=_model_cache_dir()
            )
    return _bm25_model


async def sparse_embed_one(text: str) -> dict:
    """Return {'indices': [...], 'values': [...]} for a single text."""
    model = await _bm25()
    embeddings = await asyncio.to_thread(lambda: list(model.embed([text])))
    emb = embeddings[0]
    return {
        "indices": [int(i) for i in emb.indices.tolist()],
        "values": [float(v) for v in emb.values.tolist()],
    }
