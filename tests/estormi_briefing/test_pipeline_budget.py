"""Pipeline wall-clock budget — env var parsing and default."""

import importlib
import os

import pytest

pytestmark = pytest.mark.unit


def test_pipeline_budget_defaults_to_zero():
    os.environ.pop("BRIEFING_WALL_CLOCK_BUDGET_S", None)
    import estormi_briefing.run_briefing as rb

    importlib.reload(rb)
    assert rb.PIPELINE_BUDGET_S == 0.0


def test_pipeline_budget_reads_env(monkeypatch):
    monkeypatch.setenv("BRIEFING_WALL_CLOCK_BUDGET_S", "300")
    import estormi_briefing.run_briefing as rb

    importlib.reload(rb)
    assert rb.PIPELINE_BUDGET_S == 300.0
