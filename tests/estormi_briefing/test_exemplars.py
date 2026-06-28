"""Exemplar bank — load/save, prompt block, harvest extraction."""

from __future__ import annotations

import json

import pytest

from estormi_briefing.compose.exemplars import (
    add_exemplars,
    exemplar_block,
    harvest_exemplars,
    load_bank,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_bank(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    yield tmp_path


def test_block_empty_without_bank():
    assert exemplar_block("lede") == ""


def test_add_dedupe_and_block_shape():
    assert add_exemplars("lede", ["Une journée en blocs enchaînés.", ""], "fable") == 1
    assert add_exemplars("lede", ["Une journée en blocs enchaînés."], "opus") == 0  # dup
    block = exemplar_block("lede")
    assert block.startswith("EXAMPLES")
    assert "DIFFERENT day" in block
    assert "- Une journée en blocs enchaînés." in block


def test_unknown_stage_rejected():
    with pytest.raises(ValueError):
        add_exemplars("world_domination", ["x"], "src")


def test_corrupt_bank_is_ignored(tmp_path):
    (tmp_path / "briefing_exemplars.json").write_text("{not json")
    assert load_bank() == {}
    assert exemplar_block("lede") == ""


def test_block_respects_char_budget():
    add_exemplars("writer", ["x" * 600, "y" * 600], "src")
    block = exemplar_block("writer")
    assert block.count("\n- ") + block.count("\n-") >= 0
    assert len(block) < 800  # one exemplar fits, the second would overflow


def test_harvest_extracts_all_four_stages():
    html = (
        "<h1>Briefing du 12 juin 2026</h1>"
        "<p>Une journée de télétravail en blocs enchaînés, pivot l'après-midi.</p>"
        "<h2>✦ Forme du jour</h2>"
        "<p>Récupération à 81 % en vert, HRV en hausse — le corps est disponible, "
        "vise plutôt la soirée pour courir.</p>"
        "<h2>📅 Ma journée</h2>"
        "<p>08:00–09:45 · créneau libre</p><p>09:45–10:00</p><p>Daily</p>"
        "<p>18:00–21:00 · créneau libre (3 h)</p>"
        "<p>Le quiz reste un « peut-être » de ta part — si tu y vas, il se termine à "
        "14:00 pile quand démarre la rétro uDP, donc décide ce matin sachant qu'il enchaîne.</p>"
        "<p> — gcal · 12 juin</p>"
        "<p>À ne pas oublier : commander les courses</p>"
        "<h2>🌍 Le monde</h2>"
        "<li>La BCE relève son taux. → Impact: ton crédit à Clichy se renchérit. "
        "[SOURCE: Le Monde]</li>"
    )
    out = harvest_exemplars(html)
    assert out["lede"] == ["Une journée de télétravail en blocs enchaînés, pivot l'après-midi."]
    assert any("81 %" in r for r in out["readiness"])
    assert out["impact"] == ["ton crédit à Clichy se renchérit"]
    assert any("peut-être" in w for w in out["writer"])
    assert not any(w.startswith("—") for w in out["writer"])


def test_bank_file_lives_in_data_dir(tmp_path):
    add_exemplars("impact", ["ton loyer à Clichy pourrait filer"], "opus")
    assert json.loads((tmp_path / "briefing_exemplars.json").read_text())
