"""Tests for memory_core/embedder.py — dense & sparse embedding with mocked models."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytestmark = pytest.mark.unit


class TestEmbedOne:
    async def test_embed_one_returns_float_list(self):
        fake_result = [np.array([0.1, 0.2, 0.3])]
        mock_model = MagicMock()
        mock_model.embed = MagicMock(return_value=iter(fake_result))

        # The embedder lives in memory_core — patch its module-level model.
        with patch("memory_core.embedder._dense_model", mock_model):
            from memory_core.embedder import embed_one

            result = await embed_one("hello")
            assert isinstance(result, list)
            assert all(isinstance(v, float) for v in result)
            assert len(result) == 3

    async def test_embed_empty_list_returns_empty(self):
        from memory_core.embedder import embed

        result = await embed([])
        assert result == []


class TestVerifyDenseDim:
    def test_real_dim_mismatch_raises(self):
        # A >=16-dim vector whose length != EMBED_DIM must fail loudly.
        with patch("memory_core.embedder._dense_dim_checked", False):
            from memory_core import embedder

            wrong = list(range(embedder.EMBED_DIM + 1))
            assert len(wrong) >= 16
            with pytest.raises(RuntimeError, match="EMBED_DIM"):
                embedder._verify_dense_dim(wrong)

    def test_toy_vector_does_not_trip_or_latch(self):
        # A <16-dim toy vector (mocked embedder) must not trip the guard — and
        # must NOT latch, so a real model embedding later in the same process is
        # still verified rather than silently skipped.
        with patch("memory_core.embedder._dense_dim_checked", False):
            from memory_core import embedder

            embedder._verify_dense_dim([0.1, 0.2, 0.3])
            assert embedder._dense_dim_checked is False


class TestSparseEmbedOne:
    async def test_sparse_embed_returns_dict(self):
        fake_sparse = MagicMock()
        fake_sparse.indices = MagicMock()
        fake_sparse.indices.tolist = MagicMock(return_value=[0, 5, 10])
        fake_sparse.values = MagicMock()
        fake_sparse.values.tolist = MagicMock(return_value=[0.8, 0.5, 0.2])

        mock_model = MagicMock()
        mock_model.embed = MagicMock(return_value=iter([fake_sparse]))

        with patch("memory_core.embedder._bm25_model", mock_model):
            from memory_core.embedder import sparse_embed_one

            result = await sparse_embed_one("test query")
            assert "indices" in result
            assert "values" in result
            assert result["indices"] == [0, 5, 10]
            assert result["values"] == [0.8, 0.5, 0.2]
            assert all(isinstance(i, int) for i in result["indices"])
            assert all(isinstance(v, float) for v in result["values"])
