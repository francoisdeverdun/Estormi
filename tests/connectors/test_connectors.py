"""Contract tests for the connector framework.

Asserts the registry catalogues every connector and validates the
extensibility surface: a new connector class registered against a custom
registry must round-trip cleanly. These tests are intended to catch
contract drift the moment someone adds a connector without filling in the
required spec fields.
"""

from __future__ import annotations

import sys
import time

import pytest

from connectors.base import (
    Connector,
    ConnectorRegistry,
    ConnectorResult,
    ConnectorSpec,
    dag_stages,
    registry,
    run_shell,
)

EXPECTED_CONNECTORS = {
    "mail",
    "notes",
    "documents",
    "gcal",
    "imessage",
    "knowledge",
    "reminders",
    "whatsapp",
    "whoop",
}


@pytest.mark.unit
def test_registry_has_all_known_connectors():
    assert EXPECTED_CONNECTORS.issubset(set(registry.list_all()))


@pytest.mark.unit
def test_registry_specs_are_well_formed():
    """Every registered connector must declare a ConnectorSpec with non-empty name, title, description."""
    for spec in registry.specs():
        assert isinstance(spec, ConnectorSpec), f"{spec!r} is not a ConnectorSpec"
        assert spec.name, "ConnectorSpec.name is empty"
        assert spec.title, f"connector {spec.name}: title is empty"
        assert spec.description, f"connector {spec.name}: description is empty"


@pytest.mark.unit
def test_connector_names_match_registry_keys():
    """Registry key must equal `spec.name` — used by URLs and metrics."""
    for name, cls in registry._connectors.items():  # noqa: SLF001
        assert cls.spec.name == name


@pytest.mark.unit
def test_duplicate_registration_raises():
    """Registering the same name twice with a different class must fail loudly."""
    reg = ConnectorRegistry()

    class A(Connector):
        spec = ConnectorSpec(name="dup", title="A", description="A")

        def ingest(self, **kwargs):
            return ConnectorResult(source="dup")

    class B(Connector):
        spec = ConnectorSpec(name="dup", title="B", description="B")

        def ingest(self, **kwargs):
            return ConnectorResult(source="dup")

    reg.register(A)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(B)


@pytest.mark.unit
def test_connector_without_spec_rejected():
    """A class without a `spec` attribute cannot register."""
    reg = ConnectorRegistry()

    class NoSpec(Connector):
        spec = None  # type: ignore[assignment]

        def ingest(self, **kwargs):
            return ConnectorResult(source="missing")

    with pytest.raises(TypeError, match="ConnectorSpec"):
        reg.register(NoSpec)


@pytest.mark.unit
def test_custom_connector_round_trip():
    """The public extensibility flow: define a spec + class, register, look up."""
    reg = ConnectorRegistry()

    @reg.register
    class MyConnector(Connector):
        spec = ConnectorSpec(
            name="my_test",
            title="My Test",
            description="Just a fixture connector for contract tests.",
        )

        def ingest(self, **kwargs):
            return ConnectorResult(source="my_test")

    assert reg.get("my_test") is MyConnector
    result = MyConnector().ingest()
    assert result.source == "my_test"
    assert result.ok


@pytest.mark.unit
def test_dag_order_unique_across_stages():
    """dag_order drives DAG stage ordering — a collision makes it non-deterministic."""
    stages = dag_stages()
    orders = [s.dag_order for s in stages]
    assert len(set(orders)) == len(orders), f"duplicate dag_order among DAG stages: {orders}"


@pytest.mark.unit
def test_watermarked_connectors_match_sources_that_set_watermark():
    """Guards the uses_watermark flag against the set of sources that actually
    call set_watermark (estormi_ingestion/*/...). A drift here mislabels the source in
    the catalogue / iOS Metrics view (e.g. WHOOP previously reported as not
    watermarked despite estormi_ingestion.whoop.sync calling set_watermark)."""
    watermarked = {s.name for s in registry.specs() if s.uses_watermark}
    assert watermarked == {
        "mail",
        "notes",
        "documents",
        "imessage",
        "knowledge",
        "reminders",
        "whoop",
    }


@pytest.mark.unit
def test_connector_result_defaults():
    r = ConnectorResult(source="test")
    assert r.errors == []
    assert r.ok is True


@pytest.mark.unit
def test_connector_result_ok_false_when_errors():
    r = ConnectorResult(source="test", errors=["boom"])
    assert r.ok is False


# ── run_shell — the executor every ingestion stage runs through ─────────────────


class TestRunShell:
    @pytest.mark.unit
    def test_success_path(self, tmp_path):
        result = run_shell(
            "ok",
            [sys.executable, "-c", "print('hello')"],
            cwd=tmp_path,
            timeout=10,
        )
        assert result.ok is True
        assert result.errors == []
        assert result.duration_ms >= 0

    @pytest.mark.unit
    def test_nonzero_exit_is_error(self, tmp_path):
        result = run_shell(
            "boom",
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            cwd=tmp_path,
            timeout=10,
        )
        assert result.ok is False
        assert len(result.errors) == 1
        assert "exit 3" in result.errors[0]

    @pytest.mark.unit
    def test_stderr_is_captured_in_failure_tail(self, tmp_path):
        # stderr is merged into stdout and tailed into the failure message.
        result = run_shell(
            "stderr",
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('OOPS-MARKER\\n'); sys.exit(1)",
            ],
            cwd=tmp_path,
            timeout=10,
        )
        assert result.ok is False
        assert "OOPS-MARKER" in result.errors[0]

    @pytest.mark.unit
    def test_launch_failure_is_reported(self, tmp_path):
        result = run_shell(
            "missing",
            [str(tmp_path / "does-not-exist-binary")],
            cwd=tmp_path,
            timeout=10,
        )
        assert result.ok is False
        assert "failed to launch" in result.errors[0]

    @pytest.mark.unit
    def test_extra_env_reaches_child(self, tmp_path):
        result = run_shell(
            "env",
            [
                sys.executable,
                "-c",
                "import os,sys; sys.exit(0 if os.environ.get('ESTORMI_TEST_VAR')=='42' else 9)",
            ],
            cwd=tmp_path,
            timeout=10,
            extra_env={"ESTORMI_TEST_VAR": "42"},
        )
        assert result.ok is True

    # NOT a pure unit test: it spawns a real ``python -c`` process that itself
    # forks a grandchild, then relies on OS process-group signalling. Mark it
    # integration so ``-m unit`` runs stay process-free and fast.
    @pytest.mark.integration
    def test_timeout_kills_backgrounded_grandchildren(self, tmp_path):
        # The child backgrounds a grandchild that, after a delay, writes a
        # sentinel file and keeps running. run_shell's timeout must kill the
        # whole process group — so the grandchild dies BEFORE it can write the
        # sentinel. Without the process-group kill the grandchild survives and
        # the sentinel appears.
        #
        # Timing budget (generous on purpose so a loaded CI runner can't flake
        # it): run_shell times out at 2s and SIGTERMs the whole group; the
        # grandchild only attempts its write after sleeping GRANDCHILD_SLEEP=8s,
        # so even a wildly delayed kill lands long before the write would. We
        # then wait past that 8s sleep before asserting the sentinel is absent,
        # leaving no window where a surviving grandchild could still be pending.
        timeout_s = 2
        grandchild_sleep_s = 8
        sentinel = tmp_path / "grandchild-ran.txt"
        script = (
            "import subprocess, sys, time, os\n"
            # Grandchild: sleep well past run_shell's timeout, then write the
            # sentinel. If the process group is killed it never gets there.
            f"gc = {sentinel.as_posix()!r}\n"
            f"child = subprocess.Popen([sys.executable, '-c',\n"
            f"    'import time, sys; time.sleep({grandchild_sleep_s}); "
            'open(sys.argv[1], "w").write("ran")\', gc])\n'
            # Parent stays alive too so run_shell hits its timeout, not a natural exit.
            "time.sleep(120)\n"
        )
        result = run_shell(
            "grandchild",
            [sys.executable, "-c", script],
            cwd=tmp_path,
            timeout=timeout_s,  # times out long before the grandchild write
        )
        assert result.ok is False
        assert f"timeout after {timeout_s}s" in result.errors[0]
        # Wait comfortably past the grandchild's sleep so that, had it survived
        # the timeout, its write would already have happened by the assert.
        time.sleep(grandchild_sleep_s + 2)
        assert not sentinel.exists(), "grandchild survived the timeout — process group not killed"
