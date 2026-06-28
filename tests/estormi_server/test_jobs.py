"""Behaviour tests for the engine-mutex / background-job module.

``server/jobs.py`` owns the engine mutex — the contract that exactly one of
the engines (ingestion / briefing) runs at a time. A break here corrupts
shared state (the local LLM, Qdrant, SQLite), so these tests exercise the
real coroutines: the mutex actually blocking a second DAG,
``stop_other_engines`` preempting the right set, job-status transitions,
failure handling, and subprocess invocation.

Subprocesses are mocked with ``AsyncMock`` around
``asyncio.create_subprocess_exec`` so nothing real is ever spawned; the fake
process exposes the attributes the production code reads (``pid``,
``returncode``, ``wait``, ``stdout``).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.server import jobs

pytestmark = pytest.mark.integration


# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_proc(pid: int = 4321, returncode: int = 0, wait_delay: float = 0.0):
    """A stand-in for ``asyncio.subprocess.Process``.

    ``returncode`` starts as ``None`` (alive) and flips to the supplied value
    once ``wait()`` is awaited — mirroring the real handle so the module's
    ``proc.returncode is None`` liveness checks behave correctly.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None
    proc.stdout = AsyncMock()
    proc._wait_calls = 0

    async def _wait():
        # Plain async function (not an AsyncMock) so calling it yields exactly
        # one coroutine — an AsyncMock with a coroutine side_effect produces a
        # second, never-awaited one and trips the suite's warning-as-error.
        proc._wait_calls += 1
        if wait_delay:
            await asyncio.sleep(wait_delay)
        proc.returncode = returncode
        return returncode

    proc.wait = _wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture(autouse=True)
def _clean_job_globals():
    """Reset module globals before and after every test.

    ``jobs`` carries process handles and the queue in module-level globals; a
    leaked handle would make a later test's liveness probe report a phantom
    "running", and a leaked queue entry would skew the reset/wake-catchup
    ordering tests.
    """
    names = (
        "_dag_proc",
        "_dag_pgid",
        "_briefing_proc",
        # Reset all three engine process-handles symmetrically (ingestion /
        # briefing / distill) so a future distill test can't leak a phantom
        # "running" into its neighbour via the liveness probe.
        "_distill_proc",
    )
    for n in names:
        setattr(jobs, n, None)
    jobs._queue.clear()
    jobs._running = None
    yield
    for n in names:
        setattr(jobs, n, None)
    jobs._queue.clear()
    jobs._running = None


async def _drain_tasks():
    """Let the ``_close_log_on_exit`` background tasks the launchers spawn
    finish, so no task dangles into the next test (the suite treats unraisable
    warnings as errors)."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(t), timeout=1)


# ── liveness probes ───────────────────────────────────────────────────────────


class TestProcessRunningProbes:
    """``_briefing_running`` prefers the in-process handle, then falls back to
    the cross-process engine lock (``_locked_alive``) — no more ``pgrep -f``."""

    async def test_running_true_from_live_handle(self):
        jobs._briefing_proc = _fake_proc()  # returncode None ⇒ alive
        assert await jobs._briefing_running() is True

    async def test_running_false_when_no_handle_and_lock_idle(self):
        jobs._briefing_proc = None
        with patch.object(jobs, "_locked_alive", new_callable=AsyncMock, return_value=False):
            assert await jobs._briefing_running() is False

    async def test_running_true_from_lock_after_restart(self):
        """No in-process handle (server restarted) but the lock names a live
        briefing owner ⇒ running."""
        jobs._briefing_proc = None
        with patch.object(jobs, "_locked_alive", new_callable=AsyncMock, return_value=True):
            assert await jobs._briefing_running() is True


# ── kill helpers ──────────────────────────────────────────────────────────────


class TestKillHelpers:
    async def test_kill_briefing_clears_handle_and_releases_lock(self):
        # The briefing child is spawned with start_new_session=True, so the kill
        # targets its whole process group (child + claude-CLI grandchildren) via
        # killpg — not a PID-only proc.terminate().
        import signal

        proc = _fake_proc()
        jobs._briefing_proc = proc
        killpg_calls: list[tuple[int, int]] = []

        def _killpg(pgid, sig):
            killpg_calls.append((pgid, sig))  # SIGTERM lands; child then exits

        with (
            patch.object(jobs, "_release_lock", new_callable=AsyncMock) as rel,
            patch.object(jobs.os, "killpg", side_effect=_killpg),
        ):
            await jobs._kill_briefing()
        assert killpg_calls and killpg_calls[0] == (proc.pid, signal.SIGTERM)
        proc.terminate.assert_not_called()
        assert jobs._briefing_proc is None
        rel.assert_awaited_once_with("briefing", proc.pid)

    async def test_kill_dag_uses_locked_pgid(self):
        """The engine lock is authoritative — its PGID is the kill target.

        Simulates the well-behaved case: SIGTERM lands, the next signal-0 probe
        finds the group gone, and no SIGKILL escalation fires. The lock is then
        released for the killed group.
        """
        import signal

        sigterm_sent = False

        def _killpg(pgid, sig):
            nonlocal sigterm_sent
            if sig == signal.SIGTERM:
                sigterm_sent = True
                return  # delivered
            if sig == 0:
                raise ProcessLookupError  # group gone after SIGTERM
            raise AssertionError(f"unexpected signal {sig}")

        with (
            patch.object(jobs, "_locked_pgid", new_callable=AsyncMock, return_value=99999),
            patch.object(jobs, "_release_lock", new_callable=AsyncMock) as rel,
            patch.object(jobs.os, "killpg", side_effect=_killpg) as killpg,
        ):
            await jobs._kill_dag_processes()
        assert sigterm_sent
        assert killpg.call_args_list[0][0] == (99999, signal.SIGTERM)
        signals_sent = [call.args[1] for call in killpg.call_args_list]
        assert signal.SIGKILL not in signals_sent
        rel.assert_awaited_once_with("ingestion", 99999)
        assert jobs._dag_proc is None and jobs._dag_pgid is None

    async def test_kill_dag_escalates_to_sigkill_on_stuck_dag(self):
        """A worker that ignores SIGTERM is force-killed before the lock is released."""
        import signal

        signals_sent: list[int] = []

        def _killpg(pgid, sig):
            signals_sent.append(sig)
            return  # signal 0 always succeeds ⇒ the loop reaches SIGKILL

        with (
            patch.object(jobs, "_locked_pgid", new_callable=AsyncMock, return_value=77777),
            patch.object(jobs, "_release_lock", new_callable=AsyncMock),
            patch.object(jobs.os, "killpg", side_effect=_killpg),
            patch.object(jobs.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await jobs._kill_dag_processes()
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent

    async def test_kill_dag_swallows_dead_pgid(self):
        """A stale PGID raising ProcessLookupError on SIGTERM must not propagate."""
        with (
            patch.object(jobs, "_locked_pgid", new_callable=AsyncMock, return_value=12345),
            patch.object(jobs, "_release_lock", new_callable=AsyncMock),
            patch.object(jobs.os, "killpg", side_effect=ProcessLookupError),
        ):
            await jobs._kill_dag_processes()  # no raise


# ── engine mutex: stop_other_engines ──────────────────────────────────────────


class TestStopOtherEngines:
    async def test_unknown_engine_is_a_noop(self):
        """An unrecognised engine name logs a warning and kills nothing."""
        with (
            patch.object(jobs, "_kill_dag_processes", new_callable=AsyncMock) as kd,
            patch.object(jobs, "_kill_briefing", new_callable=AsyncMock) as kb,
        ):
            await jobs.stop_other_engines("nonsense")
        for m in (kd, kb):
            m.assert_not_awaited()

    @pytest.mark.parametrize(
        "current,spared",
        [
            ("ingestion", "_kill_dag_processes"),
            ("briefing", "_kill_briefing"),
            ("distill", "_kill_distill"),
        ],
    )
    async def test_spares_current_kills_the_others(self, current, spared):
        """The mutex: launching engine X stops exactly the other engines.

        Distill is included because wrongly preempting it kills a multi-hour
        QLoRA retrain — the most expensive engine to tear down by mistake.
        """
        killers = {
            "_kill_dag_processes": AsyncMock(),
            "_kill_briefing": AsyncMock(),
            "_kill_distill": AsyncMock(),
        }
        with (
            patch.object(jobs, "_kill_dag_processes", killers["_kill_dag_processes"]),
            patch.object(jobs, "_kill_briefing", killers["_kill_briefing"]),
            patch.object(jobs, "_kill_distill", killers["_kill_distill"]),
        ):
            await jobs.stop_other_engines(current)

        for name, mock in killers.items():
            if name == spared:
                mock.assert_not_awaited()
            else:
                mock.assert_awaited_once()

    async def test_one_killer_failing_does_not_block_the_rest(self):
        """Each kill is independently guarded — a crash in one still lets the
        others run, so the slot is always cleared."""
        ok_brief = AsyncMock()
        with (
            patch.object(
                jobs,
                "_kill_dag_processes",
                new_callable=AsyncMock,
                side_effect=RuntimeError("dag kill blew up"),
            ),
            patch.object(jobs, "_kill_briefing", ok_brief),
        ):
            # "reset" fires both killers, so the failing dag kill must not
            # stop the surviving briefing kill from running.
            await jobs.stop_other_engines("reset")  # no raise

        ok_brief.assert_awaited_once()  # the failing killer did not block it


# ── engine mutex: stop_engine ─────────────────────────────────────────────────


class TestStopEngine:
    @pytest.mark.parametrize(
        "kind,killer",
        [
            ("ingestion", "_kill_dag_processes"),
            ("briefing", "_kill_briefing"),
            ("distill", "_kill_distill"),
        ],
    )
    async def test_kills_only_the_targeted_engine(self, kind, killer):
        """Stop kills the kind's processes and leaves the others alone.

        The Engine Room's Stop button hits ``/api/jobs/stop`` which calls
        this helper — getting the wrong engine would tear down an
        innocent run (distill included: it is the costliest to kill wrongly).
        """
        mocks = {
            "_kill_dag_processes": AsyncMock(),
            "_kill_briefing": AsyncMock(),
            "_kill_distill": AsyncMock(),
        }
        with (
            patch.object(jobs, "_kill_dag_processes", mocks["_kill_dag_processes"]),
            patch.object(jobs, "_kill_briefing", mocks["_kill_briefing"]),
            patch.object(jobs, "_kill_distill", mocks["_kill_distill"]),
        ):
            await jobs.stop_engine(kind)

        for name, mock in mocks.items():
            if name == killer:
                mock.assert_awaited_once()
            else:
                mock.assert_not_awaited()


# ── engine mutex: _run_dag ────────────────────────────────────────────────────


class TestRunDagMutex:
    async def test_second_dag_defers_while_lock_held(self):
        """The mutex actually blocks: a second _run_dag while the lock is held
        bails immediately instead of spawning a process."""
        await jobs._dag_lock.acquire()
        try:
            with (
                patch.object(jobs, "stop_other_engines", new_callable=AsyncMock) as soe,
                patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            ):
                await jobs._run_dag()
            # Lock was held ⇒ neither preemption nor spawn happened.
            soe.assert_not_awaited()
            spawn.assert_not_called()
        finally:
            jobs._dag_lock.release()

    async def test_run_dag_preempts_then_runs_when_lock_free(self, tmp_path):
        """A free lock ⇒ DAG preempts the other engines and spawns the script."""
        log = tmp_path / "dag.log"
        err = tmp_path / "dag-err.log"
        proc = _fake_proc(pid=777)
        snapshot = {"source_notes_enabled": "true"}

        with (
            patch.object(jobs, "_DAG_MAIN_LOG", log),
            patch.object(jobs, "_DAG_ERR_LOG", err),
            patch.object(jobs, "ROOT", tmp_path),
            patch.object(jobs, "_settings_snapshot", AsyncMock(return_value=snapshot)),
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock) as soe,
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as spawn,
        ):
            await jobs._run_dag()

        soe.assert_awaited_once_with("ingestion")
        spawn.assert_called_once()
        # bash <repo>/scripts/daily_ingestion.sh
        assert spawn.call_args[0][0] == "bash"
        # STAGES env names only the enabled source.
        assert spawn.call_args[1]["env"]["STAGES"] == "notes"
        # Globals cleared in the finally block once the run finished.
        assert jobs._dag_proc is None and jobs._dag_pgid is None
        assert not jobs._dag_lock.locked()

    async def test_run_dag_skips_when_no_source_enabled(self, tmp_path):
        """All sources off ⇒ no process is spawned (no-op run avoided)."""
        with (
            patch.object(jobs, "ROOT", tmp_path),
            patch.object(jobs, "_settings_snapshot", AsyncMock(return_value={})),
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        ):
            await jobs._run_dag()
        spawn.assert_not_called()
        assert not jobs._dag_lock.locked()

    async def test_run_dag_rejects_disabled_stage_override(self, tmp_path):
        """A stage_override for a toggled-off source must not spawn anything."""
        with (
            patch.object(jobs, "ROOT", tmp_path),
            patch.object(
                jobs,
                "_settings_snapshot",
                AsyncMock(return_value={"source_notes_enabled": "true"}),
            ),
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        ):
            # "mail" is not enabled in the snapshot ⇒ rejected.
            await jobs._run_dag(stage_override="mail")
        spawn.assert_not_called()

    async def test_run_dag_honours_enabled_stage_override(self, tmp_path):
        """A stage_override for an enabled source narrows STAGES to it."""
        log = tmp_path / "dag.log"
        err = tmp_path / "dag-err.log"
        proc = _fake_proc()
        snapshot = {
            "source_notes_enabled": "true",
            "source_mail_enabled": "true",
        }
        with (
            patch.object(jobs, "_DAG_MAIN_LOG", log),
            patch.object(jobs, "_DAG_ERR_LOG", err),
            patch.object(jobs, "ROOT", tmp_path),
            patch.object(jobs, "_settings_snapshot", AsyncMock(return_value=snapshot)),
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as spawn,
        ):
            await jobs._run_dag(stage_override="mail")
        assert spawn.call_args[1]["env"]["STAGES"] == "mail"

    async def test_run_dag_clears_globals_even_when_proc_fails(self, tmp_path):
        """A non-zero DAG exit must still clear the handle globals."""
        proc = _fake_proc(returncode=1)
        with (
            patch.object(jobs, "_DAG_MAIN_LOG", tmp_path / "d.log"),
            patch.object(jobs, "_DAG_ERR_LOG", tmp_path / "e.log"),
            patch.object(jobs, "ROOT", tmp_path),
            patch.object(
                jobs,
                "_settings_snapshot",
                AsyncMock(return_value={"source_notes_enabled": "true"}),
            ),
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ),
        ):
            await jobs._run_dag()
        assert jobs._dag_proc is None and jobs._dag_pgid is None


# ── launchers ─────────────────────────────────────────────────────────────────


class TestLaunchers:
    async def test_launch_briefing_preempts_other_engines(self):
        proc = _fake_proc()
        with (
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock) as soe,
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as spawn,
        ):
            await jobs._launch_briefing()
            assert jobs._briefing_proc is proc
            await _drain_tasks()
        soe.assert_awaited_once_with("briefing")
        assert "estormi_briefing.run_briefing" in spawn.call_args[0]


# ── queue primitives ──────────────────────────────────────────────────────────
# (queue + runner-tracked-kind reset is handled by the single autouse
# ``_clean_job_globals`` fixture above — it already clears ``_queue`` and
# ``_running`` alongside the process handles.)


class TestEnqueue:
    """``enqueue`` is the single mutator the rest of the system depends on
    — dedupe by kind is the load-bearing invariant that keeps the queue
    from blowing up under a backlog watcher that fires every minute."""

    async def test_enqueue_adds_entry_and_returns_queued(self):
        result = await jobs.enqueue("ingestion", "manual")
        assert result == "queued"
        snap = jobs.queue_snapshot()
        assert [e["kind"] for e in snap] == ["ingestion"]
        assert snap[0]["source"] == "manual"

    async def test_enqueue_is_dedupe_by_kind(self):
        """A second enqueue of the same kind is a no-op (already_queued)."""
        await jobs.enqueue("ingestion", "manual")
        result = await jobs.enqueue("ingestion", "backlog")
        assert result == "already_queued"
        assert len(jobs._queue) == 1

    async def test_enqueue_rejects_when_running(self):
        """An attempt to enqueue the kind that's currently dispatched is
        reported as already_running so the caller can show a useful status."""
        jobs._running = "briefing"
        result = await jobs.enqueue("briefing", "manual")
        assert result == "already_running"
        assert jobs._queue == []

    async def test_enqueue_reconciles_stale_bus_state(self):
        """If the event bus is tracking a kind as running but the queue
        runner doesn't own it (``_running`` is None), the bus state is stale
        — a previous ``emit_stopped`` got swallowed by a preemption swap.
        Without reconciliation every future enqueue for that kind returns
        ``already_running`` forever and only a server restart unsticks it.
        """
        from estormi_server.server import events as engine_events

        try:
            engine_events.emit_started("ingestion")
            assert engine_events.current_kind() == "ingestion"
            assert jobs._running is None

            result = await jobs.enqueue("ingestion", "manual")
            assert result == "queued"
            assert engine_events.current_kind() is None
            assert [e.kind for e in jobs._queue] == ["ingestion"]
        finally:
            engine_events.force_clear_current()

    async def test_clear_queue_drops_waiting_entries(self):
        await jobs.enqueue("ingestion", "manual")
        await jobs.enqueue("briefing", "manual")
        n = await jobs.clear_queue()
        assert n == 2
        assert jobs._queue == []

    async def test_clear_queue_leaves_running_untouched(self):
        """Clear must only drain the waiting list — the running engine is
        unaffected (the user pressed Clear, not Stop)."""
        jobs._running = "ingestion"
        await jobs.enqueue("briefing", "manual")
        await jobs.clear_queue()
        assert jobs._running == "ingestion"

    async def test_remove_from_queue_drops_matching_entry(self):
        await jobs.enqueue("ingestion", "manual")
        await jobs.enqueue("briefing", "manual")
        removed = await jobs.remove_from_queue("ingestion")
        assert removed is True
        assert [e["kind"] for e in jobs.queue_snapshot()] == ["briefing"]

    async def test_remove_from_queue_noop_when_absent(self):
        await jobs.enqueue("briefing", "manual")
        removed = await jobs.remove_from_queue("ingestion")
        assert removed is False
        assert [e["kind"] for e in jobs.queue_snapshot()] == ["briefing"]

    async def test_remove_from_queue_leaves_running_untouched(self):
        """``_running`` is the in-flight engine, not a queue entry — remove
        must not clear it, otherwise enqueue dedupe is wrong while the
        engine is still executing."""
        jobs._running = "ingestion"
        removed = await jobs.remove_from_queue("ingestion")
        assert removed is False
        assert jobs._running == "ingestion"


# ── events-bus transitions the queue dispatch relies on ───────────────────────
# The queue runner and the /api/jobs routes read the events bus (re-exported via
# ``engine_events``) for "what's running". Two transitions are load-bearing for
# the mutex contract and are exercised here in the jobs context (the full bus
# unit coverage lives in ``test_events.py``): the preemption synthetic-stop that
# keeps the snapshot consistent when one engine SIGTERMs another, and the
# kind-mismatch no-op that stops a late/duplicate stop from clobbering a
# freshly-started engine.


class TestEventsBusTransitions:
    async def test_emit_started_preempts_other_kind_with_synthetic_stop(self, _reset_engine_bus):
        """Starting a different engine while one is tracked emits a synthetic
        ``cancelled`` stop for the old one before the new start — the mutex
        preempts via SIGTERM and the old worker's real ``emit_stopped`` may not
        have landed yet, so the bus heals the gap itself."""
        from estormi_server.server import events as engine_events

        captured: list[dict] = []
        with patch.object(engine_events, "_publish", side_effect=captured.append):
            engine_events.emit_started("ingestion", started_at=1.0)
            engine_events.emit_started("briefing", started_at=2.0)  # preempts

        types = [(e["type"], e.get("kind"), e.get("status")) for e in captured]
        assert types == [
            ("engine.started", "ingestion", None),
            ("engine.stopped", "ingestion", "cancelled"),  # synthetic preemption stop
            ("engine.started", "briefing", None),
        ]
        assert engine_events.current_kind() == "briefing"
        assert engine_events._last_kind == "ingestion"
        assert engine_events._last_status == "cancelled"

    async def test_emit_stopped_for_wrong_kind_is_a_noop(self, _reset_engine_bus):
        """A stop for a kind that isn't the tracked one (a duplicate stop, or a
        preemption that already swapped the tracked kind) must change nothing and
        publish nothing — otherwise it would clear a freshly-started engine and
        wedge the queue runner on a cleared idle event."""
        from estormi_server.server import events as engine_events

        engine_events.emit_started("briefing", started_at=1.0)
        idle_cleared = not engine_events.engine_idle_event().is_set()
        captured: list[dict] = []
        with patch.object(engine_events, "_publish", side_effect=captured.append):
            engine_events.emit_stopped("ingestion", status="ok")  # not tracked

        assert captured == []  # nothing broadcast
        assert engine_events.current_kind() == "briefing"  # tracked kind intact
        assert engine_events._last_kind is None  # no spurious "last" recorded
        assert (not engine_events.engine_idle_event().is_set()) == idle_cleared


# ── idle-wait self-healing (stale-bus wedge fix) ──────────────────────────────


@pytest.fixture
def _reset_engine_bus():
    """Rebind the engine-idle ``Event`` to the running loop + clear bus state.

    ``events._engine_idle_event`` is created at import and lazily binds to the
    first loop that awaits it; pytest-asyncio's per-test loops then make a later
    ``.wait()`` raise "bound to a different event loop". Rebinding per test keeps
    the runner's idle-wait tests loop-clean (a non-issue in production, where the
    server owns a single long-lived loop). Also resets the tracked-kind so a
    leaked ``emit_started`` can't bleed into the next test.
    """
    from estormi_server.server import events as engine_events

    def _reset():
        engine_events._engine_idle_event = asyncio.Event()
        engine_events._engine_idle_event.set()
        engine_events._current_kind = None
        engine_events._current_started_at = None
        # Also clear the "last finished" trio so a leaked emit from a prior test
        # can't bleed into a snapshot/`_last_*` assertion here.
        engine_events._last_kind = None
        engine_events._last_status = None
        engine_events._last_ended_at = None

    _reset()
    yield
    _reset()


class TestEngineProcessAlive:
    """``_engine_process_alive`` is the read-only ground-truth probe the runner
    uses to tell a long-running engine apart from a stale idle event. It must
    err toward ``alive`` so a real engine is never healed away."""

    async def test_briefing_delegates_to_briefing_running(self):
        with patch.object(jobs, "_briefing_running", new_callable=AsyncMock, return_value=True):
            assert await jobs._engine_process_alive("briefing") is True
        with patch.object(jobs, "_briefing_running", new_callable=AsyncMock, return_value=False):
            assert await jobs._engine_process_alive("briefing") is False

    async def test_ingestion_alive_when_proc_handle_running(self):
        jobs._dag_proc = _fake_proc()  # returncode None ⇒ alive
        assert await jobs._engine_process_alive("ingestion") is True

    async def test_ingestion_dead_when_no_proc_and_lock_idle(self):
        jobs._dag_proc = None
        with patch.object(jobs, "_locked_alive", new_callable=AsyncMock, return_value=False):
            assert await jobs._engine_process_alive("ingestion") is False

    async def test_ingestion_liveness_via_lock(self):
        """With no in-process handle, the engine lock is the ground truth:
        a live ingestion owner ⇒ alive, an absent/dead one ⇒ dead."""
        jobs._dag_proc = None
        with patch.object(jobs, "_locked_alive", new_callable=AsyncMock, return_value=True):
            assert await jobs._engine_process_alive("ingestion") is True
        with patch.object(jobs, "_locked_alive", new_callable=AsyncMock, return_value=False):
            assert await jobs._engine_process_alive("ingestion") is False


class TestAwaitEngineIdle:
    """The bounded, self-healing idle wait that replaced the unbounded
    ``engine_idle_event().wait()`` in the runner."""

    async def test_returns_immediately_when_idle(self, _reset_engine_bus):
        from estormi_server.server import events as engine_events

        engine_events.engine_idle_event().set()  # server baseline: idle
        await asyncio.wait_for(jobs._await_engine_idle(), timeout=1)  # no hang

    async def test_heals_when_owner_confirmed_dead(self, _reset_engine_bus):
        """A stale bus (engine tracked + idle cleared) whose engine is confirmed
        dead is healed: bus cleared, idle event re-set, wait returns."""
        from estormi_server.server import events as engine_events

        try:
            engine_events.emit_started("briefing")  # clears idle, bus=briefing
            jobs._running = None
            assert not engine_events.engine_idle_event().is_set()
            with (
                patch.object(jobs, "_IDLE_RECHECK_SECS", 0.01),
                patch.object(
                    jobs, "_engine_process_alive", new_callable=AsyncMock, return_value=False
                ),
            ):
                await asyncio.wait_for(jobs._await_engine_idle(), timeout=2)
            assert engine_events.current_kind() is None
            assert engine_events.engine_idle_event().is_set()
        finally:
            engine_events.force_clear_current()
            engine_events.engine_idle_event().set()

    async def test_keeps_waiting_while_alive_then_returns_on_stop(self, _reset_engine_bus):
        """A genuinely running engine is never healed away: the wait blocks
        across recheck cycles while the probe says alive, and only returns when
        the real ``emit_stopped`` finally sets the idle event."""
        from estormi_server.server import events as engine_events

        try:
            engine_events.emit_started("ingestion")  # clears idle
            jobs._running = "ingestion"
            with (
                patch.object(jobs, "_IDLE_RECHECK_SECS", 0.01),
                patch.object(
                    jobs, "_engine_process_alive", new_callable=AsyncMock, return_value=True
                ),
            ):
                waiter = asyncio.create_task(jobs._await_engine_idle())
                await asyncio.sleep(0.05)  # several recheck cycles elapse
                assert not waiter.done()  # alive ⇒ still waiting, not healed
                engine_events.emit_stopped("ingestion")  # the real stop lands
                await asyncio.wait_for(waiter, timeout=2)
            assert waiter.done() and waiter.exception() is None
        finally:
            engine_events.force_clear_current()
            engine_events.engine_idle_event().set()
            jobs._running = None


class TestRunnerStaleBusHealing:
    """The headline residual: a different-kind enqueue against a stale bus used
    to wedge the queue runner on the idle event forever (only a restart unstuck
    it). The runner now self-heals and dispatches the new kind."""

    async def test_runner_heals_dead_stale_bus_and_dispatches_different_kind(
        self, _reset_engine_bus
    ):
        from estormi_server.server import events as engine_events

        dispatched: list[str] = []

        async def fake_dispatch(entry):
            dispatched.append(entry.kind)

        try:
            # Lost-stop state: briefing tracked + idle cleared, but no subprocess
            # owns it and the runner doesn't either (_running is None).
            engine_events.emit_started("briefing")
            jobs._running = None
            with (
                patch.object(jobs, "_IDLE_RECHECK_SECS", 0.01),
                patch.object(
                    jobs, "_engine_process_alive", new_callable=AsyncMock, return_value=False
                ),
                patch.object(jobs, "_dispatch", side_effect=fake_dispatch),
            ):
                runner = asyncio.create_task(jobs._queue_runner())
                try:
                    await jobs.enqueue("ingestion", "manual")
                    # Old code: runner blocks forever, dispatched stays empty and
                    # this loop expires → the assert below fails (no hang).
                    for _ in range(200):  # ≤ ~2 s
                        if dispatched:
                            break
                        await asyncio.sleep(0.01)
                finally:
                    runner.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await runner
            assert dispatched == ["ingestion"]
        finally:
            engine_events.force_clear_current()
            engine_events.engine_idle_event().set()
            jobs._running = None


class TestScheduleWrappers:
    """The scheduler doesn't call launchers directly anymore — it enqueues
    with ``source='schedule'`` so the queue runner is the only dispatcher."""

    async def test_schedule_ingestion_enqueues_with_schedule_source(self):
        await jobs._schedule_ingestion()
        assert len(jobs._queue) == 1
        assert jobs._queue[0].kind == "ingestion"
        assert jobs._queue[0].source == "schedule"

    async def test_schedule_briefing_enqueues_with_schedule_source(self):
        await jobs._schedule_briefing()
        assert len(jobs._queue) == 1
        assert jobs._queue[0].kind == "briefing"
        assert jobs._queue[0].source == "schedule"


# ── env-override helpers ──────────────────────────────────────────────────────


class TestIngestEnvOverrides:
    def test_documents_root_applied(self):
        env: dict[str, str] = {}
        jobs.apply_ingest_env_overrides(env, {"documents_root": "/docs"})
        assert env["DOCUMENTS_ROOT"] == "/docs"

    def test_depth_window_falls_back_to_default(self):
        """An unset historic_depth gets each source's own default window.

        That is the universal 90d for personal sources, but a source may
        declare a shorter ``default_depth`` (e.g. ``knowledge`` → ``1w``, so a
        fresh install doesn't pull months of transcripts/articles on day one).
        """
        env: dict[str, str] = {}
        jobs.apply_ingest_env_overrides(env, {})
        assert env  # at least one depth env var was written
        for key, env_var in jobs._DEPTH_ENV.items():
            fallback = jobs._DEPTH_DEFAULTS.get(key, jobs._DEFAULT_DEPTH)
            assert env[env_var] == jobs._DEPTH_TO_DAYS[fallback]

    def test_explicit_depth_pick_is_translated(self):
        """A real depth pick is translated to its day count for the source."""
        env: dict[str, str] = {}
        depth_key, depth_env = next(iter(jobs._DEPTH_ENV.items()))
        jobs.apply_ingest_env_overrides(env, {f"{depth_key}_historic_depth": "1y"})
        assert env[depth_env] == jobs._DEPTH_TO_DAYS["1y"]


class TestStageRunnable:
    def test_toggled_off_source_is_not_runnable(self):
        assert jobs._stage_runnable("notes", {}) is False
        assert jobs._stage_runnable("notes", {"source_notes_enabled": "false"}) is False

    def test_toggled_on_plain_source_is_runnable(self):
        assert jobs._stage_runnable("notes", {"source_notes_enabled": "true"}) is True

    def test_registry_declares_at_least_one_root_required_stage(self):
        """The product ships ``documents`` as a root-required source, so the
        registry-derived ``_ROOT_REQUIRED_STAGES`` is never empty. Pinning that
        here keeps the runnability test below honest — if the set ever went
        empty, that test would silently assert nothing.
        """
        assert jobs._ROOT_REQUIRED_STAGES, "registry must declare a root-required stage"
        assert "documents" in jobs._ROOT_REQUIRED_STAGES

    @pytest.mark.parametrize("key", sorted(jobs._ROOT_REQUIRED_STAGES))
    def test_root_required_source_needs_its_root(self, key):
        """A root-required stage is un-runnable when enabled-but-rootless, and
        becomes runnable only once its ``<key>_root`` is set. Parametrised over
        every root-required stage in the registry so no stage is left uncovered.
        """
        on_only = {f"source_{key}_enabled": "true"}
        assert jobs._stage_runnable(key, on_only) is False
        # A blank/whitespace root is still "not set up".
        assert jobs._stage_runnable(key, {**on_only, f"{key}_root": "  "}) is False
        with_root = {**on_only, f"{key}_root": "/some/path"}
        assert jobs._stage_runnable(key, with_root) is True


class TestSettingsSnapshot:
    async def test_snapshot_reads_all_settings_rows(self, db):
        await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("k1", "v1"))
        await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("k2", "v2"))
        await db.commit()
        with patch("estormi_server.storage.tools.sqlite_conn", return_value=db):
            snap = await jobs._settings_snapshot()
        assert snap["k1"] == "v1" and snap["k2"] == "v2"


class TestBuildTimeseries:
    def test_drops_zero_sources_and_sums_total(self):
        block = jobs._build_timeseries(
            ["2026-05-29", "2026-05-30"],
            {"2026-05-29": {"mail": 3, "notes": 0}, "2026-05-30": {"mail": 1, "notes": 2}},
            ["mail", "notes"],
        )
        assert block["days"] == ["2026-05-29", "2026-05-30"]
        # Zero-valued sources are dropped from a day's by_source map.
        assert block["series"][0]["by_source"] == {"mail": 3}
        assert block["series"][0]["total"] == 3
        assert block["series"][1]["by_source"] == {"mail": 1, "notes": 2}
        assert block["series"][1]["total"] == 3


class TestBuildVaultMetrics:
    async def _seed(self, db):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            ("c1", "h1", "notes", "personal", today),
            ("c2", "h2", "notes", "personal", today),
            ("c3", "h3", "notes", "personal", today),
            ("c4", "h4", "mail", "personal", today),
            ("c5", "h5", "mail", "personal", today),
            ("c6", "h6", "rss", "world", today),
        ]
        for cid, h, source, corpus, ts in rows:
            await db.execute(
                "INSERT INTO chunks (id, content_hash, source, corpus, ingested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cid, h, source, corpus, ts),
            )
        await db.commit()

    async def test_snapshot_shape(self, db):
        await self._seed(db)
        with patch("estormi_server.storage.tools.sqlite_conn", return_value=db):
            metrics = await jobs._build_vault_metrics()

        assert metrics is not None
        assert metrics["totalChunks"] == 6
        assert metrics["bySource"] == {"notes": 3, "mail": 2, "rss": 1}
        assert metrics["corpus"] == {"personal": 5, "world": 1}

        # Ingestion deltas cover today; window sum equals everything ingested.
        ingest_total = sum(p["total"] for p in metrics["ingestion"]["series"])
        assert ingest_total == 6
        # Memory is cumulative — the last day lands on the all-time totals.
        assert metrics["memory"]["series"][-1]["total"] == 6

        # Source catalogue is the connector registry, busiest first.
        names = [s["name"] for s in metrics["sources"]]
        assert "notes" in names and "mail" in names
        notes = next(s for s in metrics["sources"] if s["name"] == "notes")
        assert notes["chunks"] == 3
        assert "title" in notes and "usesWatermark" in notes


# ── admin reset drains the queue before stopping engines (sweep 2 U1) ─────────


def _make_reset_event_mocks():
    """Return mocks for the server.events and tools modules used by resets."""
    idle_event = MagicMock()
    idle_event.wait = AsyncMock()
    events_mod = MagicMock()
    events_mod.engine_idle_event.return_value = idle_event
    events_mod.force_clear_current = MagicMock()
    return events_mod, idle_event


class TestAdminResetSettingsDrainsQueueFirst:
    """``admin_reset_settings`` must drain the queue before killing engines.

    The queue runner parks on ``engine_idle_event().wait()``. If a reset stops
    the engines without first draining the queue, the stopped engine's teardown
    fires the idle event, the runner wakes, pops the queued entry, and dispatches
    a new engine into the DB tables the reset is mid-truncating (bug U1)."""

    async def test_queue_empty_when_stop_called(self):
        await jobs.enqueue("briefing", "schedule")
        assert len(jobs._queue) == 1

        call_order: list[str] = []
        queue_state_on_stop: list[int] = []

        async def _fake_clear_queue():
            call_order.append("clear_queue")
            jobs._queue.clear()
            return 1

        async def _fake_stop(reason: str):
            call_order.append("stop_other_engines")
            queue_state_on_stop.append(len(jobs._queue))

        sqlite_mock = AsyncMock()
        sqlite_mock.execute = AsyncMock()
        sqlite_mock.commit = AsyncMock()

        with (
            patch.object(jobs, "clear_queue", _fake_clear_queue),
            patch.object(jobs, "stop_other_engines", _fake_stop),
            patch("estormi_server.storage.tools.sqlite_conn", return_value=sqlite_mock),
        ):
            import estormi_server.api.admin as admin_mod  # noqa: PLC0415

            with patch("memory_core.audit.log_security_decision", MagicMock()):
                request = MagicMock()
                request.client = MagicMock()
                request.client.host = "127.0.0.1"
                try:
                    await admin_mod.admin_reset_settings(request)
                except Exception:
                    pass

        assert "clear_queue" in call_order, "clear_queue was never called"
        assert "stop_other_engines" in call_order, "stop_other_engines was never called"
        cq_idx = call_order.index("clear_queue")
        soe_idx = call_order.index("stop_other_engines")
        assert cq_idx < soe_idx, (
            f"clear_queue (pos {cq_idx}) must precede stop_other_engines (pos {soe_idx})"
        )
        assert queue_state_on_stop == [0], (
            f"queue was not empty when stop_other_engines fired; size was {queue_state_on_stop}"
        )


class TestAdminResetDrainsQueueFirst:
    """``admin_reset`` must drain the queue before killing engines."""

    async def test_queue_empty_when_stop_called(self):
        await jobs.enqueue("ingestion", "schedule")
        await jobs.enqueue("briefing", "manual")
        assert len(jobs._queue) == 2

        call_order: list[str] = []
        queue_state_on_stop: list[int] = []

        async def _fake_clear_queue():
            call_order.append("clear_queue")
            jobs._queue.clear()
            return 2

        async def _fake_stop(reason: str):
            call_order.append("stop_other_engines")
            queue_state_on_stop.append(len(jobs._queue))

        sqlite_mock = AsyncMock()
        sqlite_mock.execute = AsyncMock()
        sqlite_mock.commit = AsyncMock()

        with (
            patch.object(jobs, "clear_queue", _fake_clear_queue),
            patch.object(jobs, "stop_other_engines", _fake_stop),
            patch("estormi_server.storage.tools.DATA_DIR", "/tmp"),
            patch(
                "estormi_server.api.admin.WA_STAGING_PATH",
                MagicMock(exists=MagicMock(return_value=False)),
            ),
        ):
            import estormi_server.api.admin as admin_mod  # noqa: PLC0415

            with (
                patch("memory_core.audit.log_security_decision", MagicMock()),
                patch("estormi_server.storage.chunk_admin.reset_db", AsyncMock()),
                patch("estormi_server.api.admin.asyncio", asyncio),
                # Stub the vault clear so it doesn't need real iCloud paths.
                patch("estormi_server.api.admin.asyncio.to_thread", AsyncMock()),
            ):
                request = MagicMock()
                request.client = MagicMock()
                request.client.host = "127.0.0.1"
                try:
                    await admin_mod.admin_reset(request)
                except Exception:
                    pass

        assert "clear_queue" in call_order, "clear_queue was never called"
        assert "stop_other_engines" in call_order, "stop_other_engines was never called"
        cq_idx = call_order.index("clear_queue")
        soe_idx = call_order.index("stop_other_engines")
        assert cq_idx < soe_idx, (
            f"clear_queue (pos {cq_idx}) must precede stop_other_engines (pos {soe_idx})"
        )
        assert queue_state_on_stop == [0], (
            f"queue was not empty when stop_other_engines fired; size was {queue_state_on_stop}"
        )


# ── _run_dag emits idle only after releasing the lock (sweep 3 D2) ────────────


class TestRunDagEmitOrder:
    """Bug D2: ``_run_dag`` fired ``emit_stopped`` (which sets the engine-idle
    event the queue runner waits on) while still holding ``_dag_lock``. The
    runner could then wake, pop the next ingestion entry, and have *its*
    ``_run_dag`` bounce off the still-held lock (``dag.skipped.already_running``)
    — silently dropping a queued run. The fix moves the emit + record outside
    the lock."""

    async def test_emit_stopped_fires_after_dag_lock_released(self, tmp_path):
        from estormi_server.server.launchers import ingestion as ing  # noqa: PLC0415

        captured: dict = {"locked_states": [], "kinds": []}
        real_emit = ing.emit_stopped

        def _spy_emit(kind, status):
            # Snapshot whether the lock is still held at every idle signal — an
            # in-lock emit (the bug) would record a True here.
            captured["locked_states"].append(jobs._dag_lock.locked())
            captured["kinds"].append(kind)
            # Keep the events bus consistent for any other test sharing the singleton.
            real_emit(kind, status)

        proc = _fake_proc()
        with (
            patch.object(jobs, "_DAG_MAIN_LOG", tmp_path / "dag.log"),
            patch.object(jobs, "_DAG_ERR_LOG", tmp_path / "dag-err.log"),
            patch.object(jobs, "ROOT", tmp_path),
            patch.object(
                jobs,
                "_settings_snapshot",
                AsyncMock(return_value={"source_notes_enabled": "true"}),
            ),
            patch.object(jobs, "stop_other_engines", new_callable=AsyncMock),
            patch.object(jobs, "_record_engine_run", new_callable=AsyncMock) as rec,
            patch.object(ing, "emit_stopped", _spy_emit),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            await jobs._run_dag()

        assert captured["kinds"] == ["ingestion"]
        # The crux: idle was signalled exactly once, with the lock already released.
        assert captured["locked_states"] == [False]
        assert not jobs._dag_lock.locked()
        # The run was still recorded (after the lock, not skipped).
        rec.assert_awaited_once()


# ── wake-catchup re-enqueues engines whose scheduled fire was missed (D1) ─────


class TestWakeCatchup:
    """Bug D1: a scheduled run whose cron time falls while the Mac is asleep is
    silently missed (APScheduler can't fire during sleep, misfire grace is 1s).
    The wake-trigger re-enqueues any engine whose most recent scheduled fire was
    missed."""

    @staticmethod
    def _run(started_at):
        from types import SimpleNamespace  # noqa: PLC0415

        return SimpleNamespace(status="ok", started_at=started_at)

    async def _run_wake(self, now, last_runs, schedules=None):
        """Drive wake_catchup with mocked settings, run history, and enqueue."""
        from datetime import timezone  # noqa: PLC0415

        schedules = schedules or {
            "schedule_cron": "0 2 * * *",
            "briefing_schedule_cron": "0 7 * * *",
        }

        async def _get_setting(key, default=""):
            return schedules.get(key, default)

        def _recent(limit=1, engine=None):
            run = last_runs.get(engine)
            return [run] if run else []

        enq = AsyncMock(return_value="queued")
        with (
            patch("estormi_server.sql.connection._get_setting", _get_setting),
            patch("memory_core.dag_state.get_recent_runs", _recent),
            patch.object(jobs, "enqueue", enq),
        ):
            result = await jobs.wake_catchup(now=now, tz=timezone.utc)
        return result, enq

    def test_most_recent_fire_today_after_fire(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

        trig = CronTrigger.from_crontab("0 7 * * *", timezone=timezone.utc)
        now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
        assert jobs._most_recent_fire(trig, now) == datetime(2026, 6, 5, 7, 0, tzinfo=timezone.utc)

    def test_most_recent_fire_before_today_fire_uses_yesterday(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

        trig = CronTrigger.from_crontab("0 7 * * *", timezone=timezone.utc)
        now = datetime(2026, 6, 5, 6, 0, tzinfo=timezone.utc)  # before 07:00
        assert jobs._most_recent_fire(trig, now) == datetime(2026, 6, 4, 7, 0, tzinfo=timezone.utc)

    async def test_missed_overnight_enqueues_both_engines(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        # Woke at 09:00; last runs were two days ago → both 02:00 and 07:00 missed.
        now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
        stale = self._run(datetime(2026, 6, 3, 2, 0, tzinfo=timezone.utc))
        result, enq = await self._run_wake(now, {"ingestion": stale, "briefing": stale})
        assert set(result) == {"ingestion", "briefing"}
        assert enq.await_count == 2

    async def test_already_ran_after_fire_is_not_re_enqueued(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        # Woke at 09:00; both engines already ran after their fires today → no-op.
        now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
        runs = {
            "ingestion": self._run(datetime(2026, 6, 5, 2, 5, tzinfo=timezone.utc)),
            "briefing": self._run(datetime(2026, 6, 5, 7, 5, tzinfo=timezone.utc)),
        }
        result, enq = await self._run_wake(now, runs)
        assert result == []
        enq.assert_not_awaited()

    async def test_partial_only_missed_engine_enqueued(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        # Woke at 08:00: ingestion (02:00) ran, but the 07:00 briefing was missed.
        now = datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc)
        runs = {
            "ingestion": self._run(datetime(2026, 6, 5, 2, 5, tzinfo=timezone.utc)),
            "briefing": self._run(datetime(2026, 6, 4, 7, 5, tzinfo=timezone.utc)),  # yesterday
        }
        result, enq = await self._run_wake(now, runs)
        assert result == ["briefing"]
        enq.assert_awaited_once_with("briefing", "schedule")

    async def test_manual_schedule_is_skipped(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
        result, enq = await self._run_wake(
            now,
            {"ingestion": None, "briefing": None},
            schedules={"schedule_cron": "manual", "briefing_schedule_cron": "manual"},
        )
        assert result == []
        enq.assert_not_awaited()


# ── HTTP router: api/jobs.py over the ASGI client ─────────────────────────────
# These drive the real route handlers through the shared ``client`` fixture
# (FastAPI app + mocked storage). They assert on the JSON contract the SPA
# depends on and exercise the error/no-op branches (unknown kind 400, stale
# stop no-op) the in-process tests above don't reach. ``current_kind()`` is the
# live ``server.events`` global, reset around each test by the
# ``_clean_job_globals`` autouse fixture's queue clears plus an explicit
# ``force_clear_current`` here so a leaked tracked-kind can't skew the routes.


@pytest.fixture
def _events_reset():
    """Clear the events bus tracked-kind before and after a router test."""
    from estormi_server.server import events as engine_events

    engine_events.force_clear_current()
    yield engine_events
    engine_events.force_clear_current()


class TestJobsQueueClearRoute:
    async def test_clear_drains_waiting_entries_and_reports_count(self, client):
        await jobs.enqueue("ingestion", "manual")
        await jobs.enqueue("briefing", "manual")
        r = await client.post("/api/jobs/queue/clear")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "cleared"
        assert body["dropped"] == 2
        assert body["queue"] == []  # snapshot reflects the drained queue
        assert jobs._queue == []

    async def test_clear_on_empty_queue_reports_zero(self, client):
        r = await client.post("/api/jobs/queue/clear")
        assert r.status_code == 200
        assert r.json()["dropped"] == 0


class TestJobsQueueRemoveRoute:
    async def test_remove_drops_named_entry(self, client):
        await jobs.enqueue("ingestion", "manual")
        await jobs.enqueue("briefing", "manual")
        r = await client.post("/api/jobs/queue/remove", json={"kind": "ingestion"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "removed"
        assert [e["kind"] for e in body["queue"]] == ["briefing"]

    async def test_remove_absent_kind_reports_not_queued(self, client):
        await jobs.enqueue("briefing", "manual")
        r = await client.post("/api/jobs/queue/remove", json={"kind": "ingestion"})
        assert r.status_code == 200
        assert r.json()["status"] == "not_queued"

    async def test_remove_unknown_kind_is_400(self, client):
        r = await client.post("/api/jobs/queue/remove", json={"kind": "nonsense"})
        assert r.status_code == 400
        assert "unknown kind" in r.json()["error"]


class TestJobsStopRoute:
    async def test_stop_unknown_kind_is_400(self, client):
        r = await client.post("/api/jobs/stop", json={"kind": "nonsense"})
        assert r.status_code == 400
        assert "unknown kind" in r.json()["error"]

    async def test_stop_when_not_the_running_kind_is_a_noop(self, client, _events_reset):
        """A stale UI Stop click for a kind that isn't the running one must not
        kill anything — it returns ``not_running`` with the actual running kind."""
        _events_reset.emit_started("ingestion")  # ingestion is what's live
        with patch.object(jobs, "stop_engine", new_callable=AsyncMock) as stop:
            r = await client.post("/api/jobs/stop", json={"kind": "briefing"})
        assert r.status_code == 200
        assert r.json() == {"status": "not_running", "running": "ingestion"}
        stop.assert_not_awaited()  # the no-op: nothing was killed

    async def test_stop_kills_the_running_kind(self, client, _events_reset):
        _events_reset.emit_started("briefing")
        with patch.object(jobs, "stop_engine", new_callable=AsyncMock) as stop:
            r = await client.post("/api/jobs/stop", json={"kind": "briefing"})
        assert r.status_code == 200
        assert r.json() == {"status": "stopped"}
        stop.assert_awaited_once_with("briefing")


class TestJobsStateRoute:
    async def test_state_reports_running_kind_and_queue(self, client, _events_reset):
        _events_reset.emit_started("distill")
        await jobs.enqueue("ingestion", "schedule")
        r = await client.get("/api/jobs/state")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] == "distill"
        assert [e["kind"] for e in body["queue"]] == ["ingestion"]

    async def test_state_idle_reports_null_running(self, client, _events_reset):
        r = await client.get("/api/jobs/state")
        assert r.status_code == 200
        assert r.json() == {"running": None, "queue": []}


class TestJobsWakeCatchupRoute:
    async def test_wake_catchup_returns_enqueued_kinds(self, client):
        with patch.object(jobs, "wake_catchup", new=AsyncMock(return_value=["briefing"])) as wake:
            r = await client.post("/api/jobs/wake-catchup")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["enqueued"] == ["briefing"]
        assert "queue" in body
        wake.assert_awaited_once()


class TestJobsScheduleRoute:
    async def test_schedule_reports_crons_and_whoop_window(self, client):
        r = await client.get("/api/jobs/schedule")
        assert r.status_code == 200
        body = r.json()
        kinds = {c["kind"] for c in body["crons"]}
        assert kinds == {"ingestion", "briefing"}
        # No scheduler jobs registered under the test app ⇒ nextRun is null.
        assert all(c["nextRun"] is None for c in body["crons"])
        whoop = body["whoopWake"]
        assert whoop["enabled"] is False  # default
        assert isinstance(whoop["windowStartHour"], int)
        assert isinstance(whoop["windowEndHour"], int)
