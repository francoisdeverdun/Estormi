"""Distill launcher lifecycle — spawn shape, lock contention, failure kill, yield.

The distillation launcher (``estormi_server/server/launchers/distill.py``) drives
a long-lived subprocess under the engine mutex and was the weakest-covered server
module (the subprocess plumbing had no dedicated test). It mirrors the briefing
launcher (see test_briefing_launcher_lifecycle.py) with one extra branch: the
child can hand its slot back with ``YIELD_EXIT_CODE`` (75), and the launcher must
record that as a clean ``ok`` and re-enqueue rather than a failure.

These tests pin, with the real subprocess/DB/lock mocked at the boundary:
  * the child is spawned as ``python -m estormi_distill.run_distill`` in its own
    session (so killpg reaps the MLX training grandchildren), from the repo root;
  * a non-"acquired" engine lock kills the child group and cancels the run;
  * a setup failure after the spawn kills the group, frees the lock, and re-raises;
  * a clean ``YIELD_EXIT_CODE`` exit is recorded ``ok`` and re-enqueued;
  * ``emit_stopped`` fires from the finally even if post-exit cleanup raises (so a
    later queued engine is never stranded).
"""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.server import jobs
from estormi_server.server.launchers import distill as distill_launcher

pytestmark = pytest.mark.integration


def _fake_proc(pid: int = 5555, exit_code: int = 0):
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    async def _wait():
        proc.returncode = exit_code
        return exit_code

    proc.wait = _wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


async def _drain_background_tasks() -> None:
    """Let the launcher's _close_log_on_exit background task settle."""
    for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=1)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolated_launcher(tmp_path):
    """Decouple the launcher from the DB dag-run recorder and the real log path."""
    jobs._distill_proc = None
    with (
        patch.object(distill_launcher, "_DISTILL_LOG", tmp_path / "distill.log"),
        patch.object(jobs, "engine_subprocess_env", return_value={}),
        patch.object(jobs, "_start_engine_dag_run", return_value=1),
        patch.object(jobs, "_finish_engine_dag_run"),
        patch.object(jobs, "_trim_log_history"),
        patch.object(jobs, "_log_size", return_value=0),
    ):
        yield
    jobs._distill_proc = None


async def test_spawn_targets_distill_module_in_new_session():
    """The child is `python -m estormi_distill.run_distill`, new session, repo cwd."""
    proc = _fake_proc()
    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as spawn,
        patch.object(jobs.engine_lock, "acquire", return_value="acquired"),
        patch.object(jobs, "_release_lock", new_callable=AsyncMock),
        patch.object(jobs, "_record_engine_run", new_callable=AsyncMock),
        patch.object(distill_launcher, "emit_started"),
        patch.object(distill_launcher, "emit_stopped"),
    ):
        await distill_launcher._launch_distill()
        await _drain_background_tasks()

    argv = spawn.call_args.args
    assert "-m" in argv and "estormi_distill.run_distill" in argv
    assert spawn.call_args.kwargs.get("start_new_session") is True
    assert spawn.call_args.kwargs.get("cwd") == str(jobs.ROOT)


async def test_lock_contention_kills_child_and_cancels():
    """A non-'acquired' lock result kills the child group and cancels the run —
    no second engine runs, nothing 'started' is emitted, the handle is cleared."""
    proc = _fake_proc()
    killpg_calls: list[tuple[int, int]] = []

    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        patch.object(jobs.engine_lock, "acquire", return_value="held"),
        patch.object(
            jobs.os, "killpg", side_effect=lambda pgid, sig: killpg_calls.append((pgid, sig))
        ),
        patch.object(jobs, "_finish_engine_dag_run") as finish,
        patch.object(distill_launcher, "emit_started") as emit_started_mock,
    ):
        await distill_launcher._launch_distill()

    assert killpg_calls and killpg_calls[0] == (proc.pid, signal.SIGTERM)
    assert any(call.args[1] == "cancelled" for call in finish.call_args_list)
    emit_started_mock.assert_not_called()
    assert jobs._distill_proc is None


async def test_setup_failure_after_spawn_kills_child_and_releases():
    """A failure after the spawn (here emit_started) kills the live child's group
    and frees the engine lock before re-raising — no orphaned training run."""
    proc = _fake_proc()
    killpg_calls: list[tuple[int, int]] = []

    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        patch.object(jobs.engine_lock, "acquire", return_value="acquired"),
        patch.object(
            jobs.os, "killpg", side_effect=lambda pgid, sig: killpg_calls.append((pgid, sig))
        ),
        patch.object(jobs, "_release_lock", new_callable=AsyncMock) as rel,
        patch.object(distill_launcher, "emit_started", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError):
            await distill_launcher._launch_distill()

    assert killpg_calls and killpg_calls[0] == (proc.pid, signal.SIGTERM)
    rel.assert_awaited_once_with("distill", proc.pid)
    assert jobs._distill_proc is None


async def test_yield_exit_code_is_recorded_ok_and_requeued():
    """A clean YIELD_EXIT_CODE (75) is a cooperative hand-off, not a failure:
    record it 'ok' and put distill back at the tail of the queue."""
    proc = _fake_proc(exit_code=distill_launcher.YIELD_EXIT_CODE)
    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        patch.object(jobs.engine_lock, "acquire", return_value="acquired"),
        patch.object(jobs, "_release_lock", new_callable=AsyncMock),
        patch.object(jobs, "_record_engine_run", new_callable=AsyncMock) as record,
        patch.object(jobs, "enqueue", new_callable=AsyncMock) as enqueue,
        patch.object(distill_launcher, "emit_started"),
        patch.object(distill_launcher, "emit_stopped") as emit_stopped_mock,
    ):
        await distill_launcher._launch_distill()
        await _drain_background_tasks()

    emit_stopped_mock.assert_called_once_with("distill", "ok")
    enqueue.assert_awaited_once_with("distill", "backlog")
    assert record.await_args.args[3] == "ok"


async def test_close_on_exit_emits_stopped_even_if_cleanup_raises():
    """emit_stopped sets the engine-idle event the queue runner blocks on, so it
    MUST fire even if post-exit cleanup raises — else later queued engines stall.
    Force an error mid-cleanup and assert emit_stopped still fires 'failed'."""
    proc = _fake_proc()
    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        patch.object(jobs.engine_lock, "acquire", return_value="acquired"),
        patch.object(jobs, "_release_lock", new_callable=AsyncMock),
        patch.object(jobs, "_record_engine_run", new_callable=AsyncMock),
        patch.object(jobs, "_engine_status_from_returncode", side_effect=RuntimeError("boom")),
        patch.object(distill_launcher, "emit_started"),
        patch.object(distill_launcher, "emit_stopped") as emit_stopped_mock,
        # The failed-status path reconciles the engine status.json; keep it off disk.
        patch("estormi_distill.paths.read_status", return_value={"phase": "failed"}),
        patch("estormi_distill.paths.write_status"),
    ):
        await distill_launcher._launch_distill()
        await _drain_background_tasks()

    emit_stopped_mock.assert_called_once_with("distill", "failed")
