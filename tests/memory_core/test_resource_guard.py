"""Tests for the resource governor and the adaptive LLM config ladder."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memory_core import llm_local
from memory_core import resource_guard as rg

pytestmark = pytest.mark.unit


# ── memory-pressure probe ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sysctl_value,expected",
    [
        ("1", rg.NORMAL),
        ("2", rg.TIGHT),
        ("4", rg.CRITICAL),
        ("", rg.NORMAL),  # probe failed — must never block work
        ("garbage", rg.NORMAL),
        ("3", rg.NORMAL),  # unknown level → treat as normal
    ],
)
def test_memory_pressure_tiers(sysctl_value, expected):
    with patch.object(rg, "_sysctl", return_value=sysctl_value):
        assert rg.memory_pressure() == expected


def test_total_ram_gb_parses_sysctl():
    with patch.object(rg, "_sysctl", return_value=str(32 * 1024**3)):
        assert rg.total_ram_gb() == pytest.approx(32.0)


def test_total_ram_gb_falls_back_when_unreadable():
    with patch.object(rg, "_sysctl", return_value=""):
        assert rg.total_ram_gb() == 16.0


# ── governor log ──────────────────────────────────────────────────────────


def test_governor_log_appends_timestamped_lines(tmp_path):
    log_path = tmp_path / "resource_guard.log"
    # _log_path() resolves at call time (so a relocated data dir is honoured);
    # patch it rather than a module-level constant.
    with patch.object(rg, "_log_path", return_value=log_path):
        rg.governor_log("first event")
        rg.governor_log("second event")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("first event")
    assert lines[1].endswith("second event")


# ── adaptive LLM config ladder ────────────────────────────────────────────


@pytest.mark.parametrize(
    "ram,expected_rung",
    [
        (64.0, 0),  # big machine → heaviest rung
        (16.0, 1),  # mid machine → one rung down
        (8.0, 3),  # small machine → well down the ladder
    ],
)
def test_start_rung_reflects_ram(ram, expected_rung):
    with patch.object(rg, "total_ram_gb", return_value=ram):
        assert llm_local._start_rung() == expected_rung


def test_start_rung_never_exceeds_the_ladder():
    # Defensive clamp: a small machine picks base rung 3, so a ladder shorter
    # than that must never be indexed past its last rung.
    with (
        patch.object(rg, "total_ram_gb", return_value=8.0),
        patch.object(llm_local, "_LLM_LADDER", [{"n_ctx": 8192}, {"n_ctx": 4096}]),
    ):
        assert llm_local._start_rung() == len(llm_local._LLM_LADDER) - 1


def test_load_with_fallback_steps_down_to_a_config_that_loads():
    """The loader walks down the ladder until a config succeeds."""
    attempts = []

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append((kwargs["n_ctx"], kwargs["n_gpu_layers"]))
            if len(attempts) < 3:
                raise MemoryError("not enough memory")

    with (
        patch.object(rg, "total_ram_gb", return_value=64.0),
        patch.object(rg, "governor_log"),
    ):
        llm = llm_local._load_with_fallback(FakeLlama, "/fake/model.gguf")
    assert isinstance(llm, FakeLlama)
    assert len(attempts) == 3  # first two rungs failed, the third loaded
    # the rung that actually loaded is recorded for callers to size work to
    assert llm_local.loaded_config() == llm_local._LLM_LADDER[2]


def test_load_with_fallback_raises_when_every_rung_fails():
    class AlwaysFails:
        def __init__(self, **kwargs):
            raise MemoryError("never fits")

    with (
        patch.object(rg, "total_ram_gb", return_value=64.0),
        patch.object(rg, "governor_log"),
        pytest.raises(RuntimeError, match="every config rung"),
    ):
        llm_local._load_with_fallback(AlwaysFails, "/fake/model.gguf")
