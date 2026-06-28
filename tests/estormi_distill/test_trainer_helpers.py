"""Unit tests for the mockable pre-flight / parsing helpers in
``estormi_distill.trainer``.

The live MLX training/fuse/eval is legitimately uncovered (it needs Apple GPU
hardware), but the surrounding pure helpers — the disk guard, the tool env, the
val-loss parser, best-checkpoint selection, the transient-fault classifier, and
the install disk-guard — are plain Python and were the bulk of this (weakest
shipped) module's uncovered lines. Pin their behaviour here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from estormi_distill import trainer

pytestmark = pytest.mark.unit


def test_free_gb_reports_positive_space(tmp_path):
    assert trainer.free_gb(tmp_path) > 0


def test_tool_env_inherits_environ_and_pins_hf_home(monkeypatch):
    monkeypatch.setenv("ESTORMI_TEST_MARKER", "x")
    env = trainer._tool_env()
    assert env["ESTORMI_TEST_MARKER"] == "x"  # inherits the parent environment
    assert env["HF_HOME"].endswith("/hf")  # pins a private HF cache


def test_best_iter_picks_lowest_loss_ties_earliest():
    assert trainer._best_iter([]) is None
    assert trainer._best_iter([[40, 1.5], [80, 1.2]]) == 80
    # Tie on loss → earliest iteration wins.
    assert trainer._best_iter([[40, 1.0], [80, 1.0]]) == 40


def test_select_best_checkpoint_promotes_existing_ckpt(tmp_path):
    adapters = tmp_path
    val_losses = [[40, 1.5], [80, 1.2]]
    # Best (80) has no checkpoint file → no-op (it's the final, unnumbered one).
    assert trainer._select_best_checkpoint(adapters, val_losses) is None
    # Now the 80-iter checkpoint exists → it is promoted to adapters.safetensors.
    (adapters / "0000080_adapters.safetensors").write_bytes(b"ckpt")
    assert trainer._select_best_checkpoint(adapters, val_losses) == 80
    assert (adapters / "adapters.safetensors").read_bytes() == b"ckpt"


def test_select_best_checkpoint_empty_is_none(tmp_path):
    assert trainer._select_best_checkpoint(tmp_path, []) is None


def test_is_transient_gpu_fault_matches_known_signatures():
    assert trainer._is_transient_gpu_fault("… Insufficient Memory …") is True
    assert trainer._is_transient_gpu_fault("Command buffer execution failed") is True
    assert trainer._is_transient_gpu_fault("ValueError: bad dataset row") is False


def test_parse_val_losses_uses_only_the_last_training_cycle():
    log = (
        "Starting training..., iters: 50\n"
        "Iter 40: Val loss 9.9\n"  # a prior, contaminating cycle
        "Starting training..., iters: 100\n"
        "Iter 40: Val loss 1.5\n"
        "Iter 80: Val loss 1.2\n"
    )
    assert trainer._parse_val_losses(log) == [[40, 1.5], [80, 1.2]]


def test_install_gguf_returns_none_when_source_missing(tmp_path):
    assert trainer.install_gguf(str(tmp_path / "nope.gguf")) is None


def test_install_gguf_bails_when_disk_is_full(tmp_path):
    src = tmp_path / "tuned.gguf"
    src.write_bytes(b"x" * 1024)
    with (
        patch.object(trainer, "resolve_data_dir", return_value=str(tmp_path)),
        patch.object(trainer, "free_gb", return_value=0.0),  # no room
    ):
        assert trainer.install_gguf(str(src)) is None
        # The source is left in place when the move is refused.
        assert src.exists()


def test_install_gguf_moves_into_models_dir(tmp_path):
    src = tmp_path / "tuned.gguf"
    src.write_bytes(b"weights")
    data_dir = tmp_path / "data"
    with (
        patch.object(trainer, "resolve_data_dir", return_value=str(data_dir)),
        patch.object(trainer, "free_gb", return_value=999.0),
    ):
        out = trainer.install_gguf(str(src))
    assert out is not None
    installed = Path(out)
    assert installed.exists()
    assert installed.read_bytes() == b"weights"
    assert not src.exists()  # moved, not copied
