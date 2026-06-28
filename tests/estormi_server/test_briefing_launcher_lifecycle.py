"""Briefing launcher lifecycle — engine-lock contention + failure kill the child.

Regression coverage for the three lifecycle fixes in
``estormi_server/server/launchers/briefing.py``:

  * the engine lock is taken right after spawn and a non-"acquired" result
    aborts the launch (kills the child) rather than running two engines at once;
  * a setup failure after the spawn kills the child's process group instead of
    leaving it running unsupervised while the engine slot is freed;
  * kills target the whole process group (start_new_session=True) so the
    ``claude`` CLI grandchildren are reaped, not orphaned.
"""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.server import jobs
from estormi_server.server.launchers import briefing as briefing_launcher

pytestmark = pytest.mark.integration


def _fake_proc(pid: int = 4321):
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    async def _wait():
        proc.returncode = 0
        return 0

    proc.wait = _wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture(autouse=True)
def _clean_briefing_proc(tmp_path):
    jobs._briefing_proc = None
    # Decouple the launcher from the DB-backed dag-run recorder + the real
    # knowledge.log path so these tests exercise only the spawn/lock/kill
    # lifecycle.
    with (
        patch.object(jobs, "_start_engine_dag_run", return_value=1),
        patch.object(jobs, "_trim_log_history"),
        patch.object(jobs, "_log_size", return_value=0),
        patch.object(jobs, "_KNOWLEDGE_LOG", tmp_path / "knowledge.log"),
    ):
        yield
    jobs._briefing_proc = None


async def test_spawn_uses_new_session_for_group_kill():
    """The child must be spawned with start_new_session=True so killpg reaches
    the claude-CLI grandchildren rather than orphaning them."""
    proc = _fake_proc()
    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as spawn,
        patch.object(jobs.engine_lock, "acquire", return_value="acquired"),
        patch.object(jobs, "_release_lock", new_callable=AsyncMock),
        patch.object(jobs, "_record_engine_run", new_callable=AsyncMock),
    ):
        await jobs._launch_briefing()
        # Let the close-on-exit task settle.
        import asyncio

        for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=1)
            except Exception:
                pass
    assert spawn.call_args.kwargs.get("start_new_session") is True


async def test_lock_contention_kills_child_and_aborts():
    """A 'held' engine lock after spawn aborts the launch: the child is killed
    (whole group) and the briefing slot is left to the existing holder."""
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
        patch.object(briefing_launcher, "emit_started") as emit_started_mock,
    ):
        await jobs._launch_briefing()

    # Child group was SIGTERM'd, the run marked cancelled, no "started" emitted,
    # and the handle cleared.
    assert killpg_calls and killpg_calls[0] == (proc.pid, signal.SIGTERM)
    assert any(call.args[1] == "cancelled" for call in finish.call_args_list)
    emit_started_mock.assert_not_called()
    assert jobs._briefing_proc is None


async def test_setup_failure_after_spawn_kills_child():
    """If a step after the spawn raises (here emit_started), the live child's
    group is killed before the engine slot is freed — no orphaned run."""
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
        patch.object(jobs, "_finish_engine_dag_run"),
        patch.object(briefing_launcher, "emit_started", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError):
            await jobs._launch_briefing()

    assert killpg_calls and killpg_calls[0] == (proc.pid, signal.SIGTERM)
    rel.assert_awaited_once_with("briefing", proc.pid)
    assert jobs._briefing_proc is None


async def test_close_on_exit_emits_stopped_even_if_cleanup_raises():
    """Regression: ``emit_stopped`` sets the engine-idle event the queue runner
    blocks on, so it MUST fire even if post-exit cleanup raises — otherwise a
    failing ``out.close()`` / ``_release_lock`` would strand every later queued
    engine forever. Force an error in the cleanup body and assert emit_stopped
    still fires (in the finally), with the fallback "failed" status."""
    import asyncio

    proc = _fake_proc()
    with (
        patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        patch.object(jobs.engine_lock, "acquire", return_value="acquired"),
        patch.object(jobs, "_release_lock", new_callable=AsyncMock),
        patch.object(jobs, "_record_engine_run", new_callable=AsyncMock),
        patch.object(jobs, "_finish_engine_dag_run"),
        patch.object(jobs, "_engine_status_from_returncode", side_effect=RuntimeError("boom")),
        patch.object(briefing_launcher, "emit_started"),
        patch.object(briefing_launcher, "emit_stopped") as emit_stopped_mock,
    ):
        await jobs._launch_briefing()
        for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=1)
            except Exception:
                pass

    emit_stopped_mock.assert_called_once_with("briefing", "failed")
