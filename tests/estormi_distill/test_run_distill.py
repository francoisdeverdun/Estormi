"""The distillation phase loop + cooperative-yield protocol (run_distill.py).

``run_distill`` must never starve the scheduled engines: at every phase boundary
it asks the server whether another engine is queued and, if so, exits with
``YIELD_EXIT_CODE`` (75) so the launcher re-enqueues it. These tests stub httpx +
the trainer/dataset/references phases so the yield contract and the end-to-end
chain are exercised without MLX or a live server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from estormi_distill import run_distill
from estormi_distill.paths import read_status

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    # write_status / read_status route through ESTORMI_DATA_DIR.
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    yield tmp_path


def _fake_httpx_client(queue):
    """An httpx.AsyncClient stand-in whose GET returns ``{"queue": queue}``."""

    class _Resp:
        def json(self):
            return {"queue": queue}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            return _Resp()

    return _Client


# ── _someone_waiting ──────────────────────────────────────────────────────────


async def test_someone_waiting_true_when_a_non_distill_engine_is_queued(monkeypatch):
    monkeypatch.setattr(
        run_distill.httpx, "AsyncClient", _fake_httpx_client([{"kind": "ingestion"}])
    )
    assert await run_distill._someone_waiting() is True


async def test_someone_waiting_false_when_only_distill_or_empty(monkeypatch):
    monkeypatch.setattr(run_distill.httpx, "AsyncClient", _fake_httpx_client([{"kind": "distill"}]))
    assert await run_distill._someone_waiting() is False
    monkeypatch.setattr(run_distill.httpx, "AsyncClient", _fake_httpx_client([]))
    assert await run_distill._someone_waiting() is False


async def test_someone_waiting_false_when_server_unreachable(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(run_distill.httpx, "AsyncClient", _Boom)
    # An unreachable server must NOT abort the run — keep working.
    assert await run_distill._someone_waiting() is False


# ── _yield_if_needed ──────────────────────────────────────────────────────────


async def test_yield_if_needed_exits_75_when_waiting(monkeypatch):
    monkeypatch.setattr(run_distill, "_someone_waiting", AsyncMock(return_value=True))
    with pytest.raises(SystemExit) as exc:
        await run_distill._yield_if_needed("train")
    assert exc.value.code == run_distill.YIELD_EXIT_CODE
    status = read_status()
    assert status["phase"] == "yielded"
    assert status["yieldedDuring"] == "train"


async def test_yield_if_needed_is_a_noop_when_idle(monkeypatch):
    monkeypatch.setattr(run_distill, "_someone_waiting", AsyncMock(return_value=False))
    # Must return normally (no SystemExit) and not stamp a yielded status.
    assert await run_distill._yield_if_needed("train") is None


# ── run() / main() ────────────────────────────────────────────────────────────


def _mock_all_phases(monkeypatch, *, gguf_pass=True):
    """Stub every phase so run() can drive the chain with no MLX/server."""
    monkeypatch.setattr(run_distill.trainer, "tooling", lambda: {"ready": True})
    monkeypatch.setattr(run_distill.references, "harvest_archive", lambda: 12)
    monkeypatch.setattr(run_distill.dataset, "build_dataset", lambda: {"train": 60, "val": 8})
    monkeypatch.setattr(run_distill.trainer, "free_gb", lambda _p: 999.0)
    monkeypatch.setattr(run_distill.trainer, "train", AsyncMock(return_value={"final_val": 1.2}))
    monkeypatch.setattr(run_distill.trainer, "evaluate", AsyncMock(return_value={"pass": True}))
    monkeypatch.setattr(
        run_distill.trainer, "fuse_to_gguf", AsyncMock(return_value="work/quill.gguf")
    )
    monkeypatch.setattr(
        run_distill.trainer, "evaluate_gguf", AsyncMock(return_value={"pass": gguf_pass})
    )
    monkeypatch.setattr(run_distill.trainer, "install_gguf", lambda _g: "ministral3-14b-estormi")
    monkeypatch.setattr(run_distill.trainer, "clear_train_marker", lambda: None)


def test_main_yields_75_at_the_first_boundary(monkeypatch):
    # Tooling ready (skip setup); someone waiting → the first _yield_if_needed
    # (harvest) trips and main() surfaces the re-enqueue code.
    monkeypatch.setattr(run_distill.trainer, "tooling", lambda: {"ready": True})
    monkeypatch.setattr(run_distill, "_someone_waiting", AsyncMock(return_value=True))
    monkeypatch.setattr(run_distill.references, "harvest_archive", MagicMock())
    assert run_distill.main() == run_distill.YIELD_EXIT_CODE
    # It yielded before harvesting anything.
    run_distill.references.harvest_archive.assert_not_called()


def test_main_runs_the_full_chain_to_done(monkeypatch):
    monkeypatch.setattr(run_distill, "_someone_waiting", AsyncMock(return_value=False))
    _mock_all_phases(monkeypatch)
    assert run_distill.main() == 0
    status = read_status()
    assert status["phase"] == "done"
    assert status["installed"] == "ministral3-14b-estormi"


def test_main_rejects_when_the_gguf_regresses(monkeypatch):
    monkeypatch.setattr(run_distill, "_someone_waiting", AsyncMock(return_value=False))
    _mock_all_phases(monkeypatch, gguf_pass=False)
    assert run_distill.main() == 1
    assert read_status()["phase"] == "rejected"
