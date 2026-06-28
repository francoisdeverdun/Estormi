"""Sidecar env hygiene: WA_TOKEN in both spawn sites, no process-wide set_var."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

MAIN_RS = Path(__file__).resolve().parents[2] / "apps" / "estormi-macos" / "src" / "main.rs"


def test_wa_token_injected_in_both_spawn_sites():
    src = MAIN_RS.read_text()
    count = src.count('.env("ESTORMI_WA_TOKEN"')
    assert count >= 2, (
        f"Expected ESTORMI_WA_TOKEN env injection in both initial spawn and "
        f"restart path, found {count} occurrence(s)"
    )


def test_no_set_var_in_main_rs():
    src = MAIN_RS.read_text()
    assert "std::env::set_var" not in src, (
        "std::env::set_var is unsafe in Rust 2024 — pass env vars via Command::env() "
        "or Axum state instead"
    )
