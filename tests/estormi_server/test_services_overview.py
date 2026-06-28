"""Unit tests for ``estormi_server.services.overview``.

``fmt_bytes`` / ``dir_size`` / ``read_version`` are side-effect-light functions
the settings-overview aggregator builds on. ``TestCachedDirSize`` covers the
TTL cache (``cached_dir_size``) regression from sweep 2 (bug U20).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from estormi_server.services import overview as svc

pytestmark = pytest.mark.unit


class TestFmtBytes:
    def test_bytes(self):
        assert svc.fmt_bytes(0) == "0 B"
        assert svc.fmt_bytes(512) == "512 B"

    def test_kilobytes(self):
        assert svc.fmt_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        assert svc.fmt_bytes(5 * 1024**2) == "5.0 MB"

    def test_gigabytes(self):
        assert svc.fmt_bytes(3 * 1024**3) == "3.00 GB"

    def test_boundary_just_under_kb(self):
        assert svc.fmt_bytes(1023) == "1023 B"


class TestDirSize:
    def test_nonexistent_path_is_zero(self, tmp_path):
        assert svc.dir_size(tmp_path / "nope") == 0

    def test_single_file(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"x" * 100)
        assert svc.dir_size(f) == 100

    def test_recursive_directory(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"x" * 10)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"y" * 25)
        assert svc.dir_size(tmp_path) == 35


class TestReadVersion:
    def test_prefers_build_version_file(self, tmp_path):
        bv = tmp_path / "packages" / "estormi_server" / "build_version.txt"
        bv.parent.mkdir(parents=True)
        bv.write_text("v9.9\n")
        with patch.object(svc, "ROOT", tmp_path):
            assert svc.read_version() == "v9.9"

    def test_falls_back_to_version_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("2.3.4\n")
        with patch.object(svc, "ROOT", tmp_path):
            assert svc.read_version() == "2.3.4"

    def test_default_when_nothing_present(self, tmp_path):
        with patch.object(svc, "ROOT", tmp_path):
            assert svc.read_version() == "1.0.0"

    def test_empty_build_version_falls_through(self, tmp_path):
        bv = tmp_path / "packages" / "estormi_server" / "build_version.txt"
        bv.parent.mkdir(parents=True)
        bv.write_text("   \n")
        (tmp_path / "VERSION").write_text("7.0\n")
        with patch.object(svc, "ROOT", tmp_path):
            assert svc.read_version() == "7.0"


class _Counter:
    """Synchronous stub that counts calls per path and returns a fixed size."""

    def __init__(self, size: int = 1234):
        self.calls: dict[Path, int] = {}
        self.size = size

    def __call__(self, p: Path) -> int:
        self.calls[p] = self.calls.get(p, 0) + 1
        return self.size

    def count(self, p: Path) -> int:
        return self.calls.get(p, 0)


class TestCachedDirSize:
    """Bug U20: ``cached_dir_size`` memoises each dir's byte count for
    ``DIR_SIZE_TTL_SECONDS`` so the 5s dashboard poll doesn't rglob+stat the
    Qdrant and staging trees on every call."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Wipe the module-level cache before and after every test."""
        svc._dir_size_cache.clear()
        yield
        svc._dir_size_cache.clear()

    async def test_cache_hit_on_second_call(self, tmp_path):
        """Two calls within the TTL → dir_size invoked only ONCE per path."""
        qdrant = tmp_path / "qdrant"
        staging = tmp_path / "staging"
        qdrant.mkdir()
        staging.mkdir()

        stub = _Counter()
        with patch.object(svc, "dir_size", stub):
            first_q = await svc.cached_dir_size(qdrant)
            second_q = await svc.cached_dir_size(qdrant)
            first_s = await svc.cached_dir_size(staging)
            second_s = await svc.cached_dir_size(staging)

        assert first_q == second_q == stub.size
        assert first_s == second_s == stub.size
        assert stub.count(qdrant.resolve()) == 1, "qdrant: expected 1 dir_size call (cache hit)"
        assert stub.count(staging.resolve()) == 1, "staging: expected 1 dir_size call (cache hit)"

    async def test_cache_miss_after_ttl_expires(self, tmp_path, monkeypatch):
        """After the TTL expires (clock advanced), the next call recomputes."""
        path = tmp_path / "qdrant"
        path.mkdir()

        stub = _Counter()
        _now = [time.monotonic()]
        monkeypatch.setattr(
            svc,
            "time",
            type("_FakeTime", (), {"monotonic": staticmethod(lambda: _now[0])})(),
        )

        with patch.object(svc, "dir_size", stub):
            await svc.cached_dir_size(path)  # call 1 — populates cache
            _now[0] += svc.DIR_SIZE_TTL_SECONDS + 1  # advance past the TTL
            await svc.cached_dir_size(path)  # call 2 — cache expired, recompute

        assert stub.count(path.resolve()) == 2, "expected 2 dir_size calls (TTL expired)"

    async def test_paths_cached_independently(self, tmp_path):
        """qdrant and staging paths are cached under distinct keys."""
        qdrant = tmp_path / "qdrant"
        staging = tmp_path / "staging"
        qdrant.mkdir()
        staging.mkdir()

        stub = _Counter(size=999)
        with patch.object(svc, "dir_size", stub):
            await svc.cached_dir_size(qdrant)
            await svc.cached_dir_size(staging)
            await svc.cached_dir_size(qdrant)
            await svc.cached_dir_size(staging)

        assert stub.count(qdrant.resolve()) == 1
        assert stub.count(staging.resolve()) == 1
