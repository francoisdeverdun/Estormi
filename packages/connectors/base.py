"""Connector framework.

A connector wraps one ingestion source (Apple Notes, WhatsApp, Google
Calendar, …) and presents a uniform contract to the rest of the system:

* a `spec` describing identity and macOS permission hints;
* an `ingest()` method that returns a typed `ConnectorResult`.

Two practical bases are provided:

* `ShellConnector` — for connectors whose ingestion is a shell script
  shipped under `estormi_ingestion/<name>/*.sh`. Handles cwd, timeouts, stderr
  capture, and structured logging.
* `ScriptConnector` — for connectors whose ingestion is a Python script
  launched out-of-process (``python -m <module>`` or ``python <script>``)
  so its argparse/sys.exit lifecycle and stdout-to-stage-log routing are
  untouched.

Adding a new connector is therefore a 20-line file plus a row in
`docs/connectors.md`. See that file for the step-by-step recipe.
"""

from __future__ import annotations

import abc
import dataclasses
import os
import subprocess
import time
from pathlib import Path
from typing import ClassVar, Sequence

# Default timeout for shell-based connectors (1 hour). Individual connectors
# can override via `ShellConnector.timeout_seconds`.
_DEFAULT_TIMEOUT = 60 * 60

# Resolved at import-time. Connector scripts are referenced relative to this.
# Override with ESTORMI_REPO_ROOT in environments where the live tree lives
# elsewhere (see estormi_server/server/jobs.py for the consumer-side resolution).
_REPO_ROOT = Path(os.getenv("ESTORMI_REPO_ROOT", "").strip() or Path(__file__).resolve().parents[2])


@dataclasses.dataclass(frozen=True)
class ConnectorSpec:
    """Static metadata for a connector. Used by the UI, docs, and CI.

    The DAG-orchestration fields (``dag_stage``, ``dag_order``,
    ``depth_window_env``, ``uses_watermark``) make this spec the single
    source of truth for ``scripts/daily_ingestion.sh`` and the
    ``sources.py`` / ``jobs.py`` maps that used to hardcode the same
    facts. See ``connectors/__main__.py``.
    """

    name: str
    """Stable identifier — used in URLs, settings keys, and metrics tags. Use snake_case."""

    title: str
    """Human-readable name shown in the Settings UI."""

    description: str
    """One-sentence summary of what this connector ingests."""

    macos_permissions: tuple[str, ...] = ()
    """macOS TCC permission keys this connector needs (e.g. 'Contacts', 'Calendars')."""

    permissions_optional: bool = False
    """Whether ``macos_permissions`` are soft hints rather than hard requirements.

    When True the preflight still prompts for them (so the dialog fires at
    activation, attributed to Estormi), but a *denied* status must NOT skip the
    stage: the connector degrades gracefully without the permission. WhatsApp is
    the canonical case — Contacts only upgrades phone-number JIDs to real names;
    without it, ingestion still runs and names fall back to push_name. See the
    run-gate in ``connectors/__main__.py``.
    """

    dag_stage: bool = False
    """Whether this connector is a stage of the daily ingestion pipeline.

    True for every connector the pipeline can run as a stage (including ``gcal``).
    Drives ``pipeline.DAG_STAGES`` and the ``connectors run`` CLI.
    """

    dag_order: int = 0
    """Position in the pipeline stage order (lower runs first). Ignored unless ``dag_stage``."""

    default_stage: bool = False
    """Whether this stage runs in the unattended nightly *run-all* pipeline.

    ``gcal`` is a pipeline stage (``dag_stage=True``) but not a default stage —
    it runs only on demand (per-source ▶ / scoped pipeline run), matching the
    historical ``DEFAULT_STAGES`` array in ``daily_ingestion.sh``.
    """

    depth_window_env: str | None = None
    """Env var the ingest script reads for its first-run history window.

    Only set for depth-capable sources (notes, mail, gcal, imessage,
    knowledge, whoop). ``None`` means the source ingests everything
    available and the Manage modal hides the depth picker. Replaces the old
    per-source depth map (now ``launchers.ingestion._DEPTH_ENV``).
    """

    default_depth: str | None = None
    """First-run historic-depth token to use when the user hasn't picked one.

    A key into ``_DEPTH_TO_DAYS`` (e.g. ``"1w"``). ``None`` falls back to the
    universal default (``90d``). News-style sources want a short default
    (``knowledge`` → ``"1w"``) so a first run doesn't pull months of feeds.
    """

    uses_watermark: bool = False
    """Whether the connector tracks progress in the ``ingestion_watermarks`` table."""

    requires_root: bool = False
    """Whether the connector needs an explicit filesystem root before it can run.

    True for the folder-rooted ``documents`` source: its root
    is stored in settings as ``<name>_root``. Until that key is set the
    connector cannot ingest, so the pipeline treats the stage as not-runnable and
    skips it rather than launching an ingester that immediately bails. See
    ``launchers.ingestion._stage_runnable`` (the root gate, invoked from
    ``_run_dag``).
    """


@dataclasses.dataclass
class ConnectorResult:
    """Outcome of a single ingestion run."""

    source: str
    errors: list[str] = dataclasses.field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors


class Connector(abc.ABC):
    """Abstract base — every concrete connector exposes a `spec` and `ingest`."""

    spec: ClassVar[ConnectorSpec]

    @abc.abstractmethod
    def ingest(self, **kwargs) -> ConnectorResult:
        """Run ingestion. Must NOT raise — surface failures via ``ConnectorResult.errors``."""


class ShellConnector(Connector):
    """Connector that delegates ingestion to a shell script.

    Subclasses override `script_path` (relative to the repo root). The base
    handles cwd, timeout, stderr capture, exit-code propagation, and timing.
    """

    script_path: ClassVar[str]
    timeout_seconds: ClassVar[int] = _DEFAULT_TIMEOUT

    def ingest(self, **kwargs) -> ConnectorResult:
        script = _REPO_ROOT / self.script_path
        if not script.is_file():
            return ConnectorResult(
                source=self.spec.name,
                errors=[f"script not found: {script}"],
            )
        return run_shell(
            self.spec.name,
            ["bash", str(script)],
            cwd=_REPO_ROOT,
            timeout=self.timeout_seconds,
        )


class ScriptConnector(Connector):
    """Connector whose ingestion is a Python script run as a subprocess.

    The script is launched out-of-process — exactly as ``daily_ingestion.sh``
    used to do — so its ``argparse`` / ``sys.exit`` lifecycle is untouched and
    its stdout streams to the per-stage log. Subclasses set ``module`` (run
    via ``python -m <module>``) OR ``script_rel`` (a path relative to the repo
    root).

    ``cwd_rel`` overrides the working directory (default: the repo root). A
    ``python -m`` connector whose package lives in a subtree — e.g. a package
    under ``estormi_ingestion/`` — sets it so the package and its sibling imports
    resolve.
    """

    module: ClassVar[str | None] = None
    script_rel: ClassVar[str | None] = None
    cwd_rel: ClassVar[str | None] = None
    timeout_seconds: ClassVar[int] = _DEFAULT_TIMEOUT

    def command(self) -> list[str]:
        """Argv for ``ingest()`` to run this Python connector as a subprocess:
        ``python -m <module>`` or ``python <script_rel>``, using the current
        interpreter so the bundled runtime is honored."""
        import sys as _sys  # noqa: PLC0415

        interp = _sys.executable
        if self.module:
            return [interp, "-m", self.module]
        if self.script_rel:
            return [interp, str(_REPO_ROOT / self.script_rel)]
        raise TypeError(f"{type(self).__name__} must set `module` or `script_rel`")

    def ingest(self, **kwargs) -> ConnectorResult:
        argv = [*self.command()]
        cwd = _REPO_ROOT / self.cwd_rel if self.cwd_rel else _REPO_ROOT
        return run_shell(
            self.spec.name,
            argv,
            cwd=cwd,
            timeout=self.timeout_seconds,
        )


def run_shell(
    source: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    extra_env: dict[str, str] | None = None,
) -> ConnectorResult:
    """Execute `argv` under `cwd` and translate the outcome to a ConnectorResult.

    The child's stdout and stderr are merged and streamed line-by-line to
    this process's stdout, flushed per line, so a parent that redirects us
    into a log file sees output live while the stage runs —
    ``daily_ingestion.sh`` does exactly that, one log per stage, and the
    Settings UI tails it. A bounded tail of the stream is also kept to
    populate the failure message.

    `extra_env` is merged on top of the inherited environment, for connectors
    that need a fixed env var set for the child process. No connector overrides
    it today; it is kept as an extensibility hook.
    """
    import collections  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    import signal as _signal  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415
    import threading  # noqa: PLC0415

    start = time.perf_counter()
    env = None
    if extra_env:
        env = {**_os.environ, **extra_env}

    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            # Put the child in its own process group (session leader) so a
            # timeout can signal the WHOLE tree. Several connectors shell out to
            # ``osascript`` / helper scripts that fork their own children; with
            # a bare ``proc.terminate()`` those grandchildren survive and keep
            # ingesting after we've given up. Signalling the group reaps them.
            start_new_session=True,
        )
    except OSError as exc:
        return ConnectorResult(
            source=source,
            errors=[f"failed to launch {argv[0]}: {exc}"],
            duration_ms=(time.perf_counter() - start) * 1000,
        )

    tail: collections.deque = collections.deque(maxlen=50)

    def _pump() -> None:
        # proc.stdout is a text stream because text=True above.
        for line in proc.stdout:  # type: ignore[union-attr]
            _sys.stdout.write(line)
            _sys.stdout.flush()
            tail.append(line)

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()

    def _signal_group(sig: int) -> None:
        # Signal the child's whole process group so backgrounded grandchildren
        # die with it. Fall back to signalling just the child if the group is
        # already gone (e.g. it exited between wait() timing out and here).
        try:
            _os.killpg(_os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Try a graceful shutdown first so the child can flush buffers and
        # tear down its own subprocesses; escalate to SIGKILL only if the group
        # refuses to exit within 5s. We signal the whole process group (the
        # child is a session leader, see start_new_session) so backgrounded
        # grandchildren are reaped too, not just the direct child.
        _signal_group(_signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_group(_signal.SIGKILL)
            proc.wait()
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
        pump.join(timeout=5)
        return ConnectorResult(
            source=source,
            errors=[f"timeout after {timeout}s"],
            duration_ms=(time.perf_counter() - start) * 1000,
        )

    pump.join(timeout=5)
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except Exception:
            pass
    duration = (time.perf_counter() - start) * 1000
    errors: list[str] = []
    if proc.returncode != 0:
        snippet = "".join(tail).strip()[-500:]
        errors.append(f"exit {proc.returncode}: {snippet}")
    return ConnectorResult(source=source, errors=errors, duration_ms=duration)


class ConnectorRegistry:
    """Single source of truth for the connector catalogue."""

    def __init__(self) -> None:
        self._connectors: dict[str, type[Connector]] = {}

    def register(self, cls: type[Connector]) -> type[Connector]:
        """Class-decorator API: `@registry.register class Foo(Connector): …`."""
        spec = getattr(cls, "spec", None)
        if not isinstance(spec, ConnectorSpec):
            raise TypeError(f"{cls.__name__} must declare a ConnectorSpec as `spec`")
        existing = self._connectors.get(spec.name)
        if existing is not None and existing is not cls:
            raise ValueError(f"connector {spec.name!r} already registered by {existing.__name__}")
        self._connectors[spec.name] = cls
        return cls

    def get(self, name: str) -> type[Connector] | None:
        return self._connectors.get(name)

    def specs(self) -> list[ConnectorSpec]:
        """All registered specs, sorted by name. Cheap — no instantiation."""
        return sorted((cls.spec for cls in self._connectors.values()), key=lambda s: s.name)

    def list_all(self) -> list[str]:
        return sorted(self._connectors.keys())


# Module-level registry. Connectors auto-register on import via the package
# `__init__`. See connectors/__init__.py.
registry = ConnectorRegistry()


def dag_stages(*, default_only: bool = False) -> list[ConnectorSpec]:
    """Registered DAG-stage specs in execution order.

    The single source of truth for ``scripts/daily_ingestion.sh`` and
    ``pipeline.DAG_STAGES``. With ``default_only`` the result is limited to
    stages that run in the unattended nightly pipeline (excludes ``gcal``).
    """
    stages = [s for s in registry.specs() if s.dag_stage and (s.default_stage or not default_only)]
    return sorted(stages, key=lambda s: s.dag_order)


__all__ = [
    "Connector",
    "ConnectorRegistry",
    "ConnectorResult",
    "ConnectorSpec",
    "ScriptConnector",
    "ShellConnector",
    "dag_stages",
    "registry",
]
