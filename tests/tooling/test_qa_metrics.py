"""Unit tests for QA metric badge generation helpers."""

from __future__ import annotations

import json

import pytest

from scripts import qa_metrics

pytestmark = pytest.mark.unit


class TestParseCollectedCount:
    def test_verbose_pytest_summary(self):
        output = "================ collected 682 items ================"
        assert qa_metrics.parse_collected_count(output) == 682

    def test_quiet_pytest_summary(self):
        output = "682 tests collected in 0.92s"
        assert qa_metrics.parse_collected_count(output) == 682


class TestDiscoverActiveLayers:
    def test_detects_layers_by_marker_regardless_of_directory(self, tmp_path):
        """A layer is active when any test file carries its marker — the file
        can live anywhere in the tree, and the result follows marker order."""
        tests_root = tmp_path / "tests"
        tests_root.mkdir()
        # Flat file marked integration; nested file marked unit; contract too.
        (tests_root / "test_flat.py").write_text(
            "import pytest\npytestmark = pytest.mark.integration\n"
        )
        (tests_root / "test_contracts.py").write_text(
            "import pytest\npytestmark = pytest.mark.contract\n"
        )
        nested = tests_root / "anywhere"
        nested.mkdir()
        (nested / "test_nested.py").write_text("import pytest\npytestmark = pytest.mark.unit\n")

        assert qa_metrics.discover_active_layers(tmp_path) == [
            "unit",
            "integration",
            "contract",
        ]

    def test_ignores_unmarked_files_and_helpers(self, tmp_path):
        tests_root = tmp_path / "tests"
        tests_root.mkdir()
        (tests_root / "test_unmarked.py").write_text("def test_probe(): pass\n")
        helpers = tests_root / "helpers"
        helpers.mkdir()
        # A marker inside helpers/ must not register as an active layer.
        (helpers / "test_helper_only.py").write_text(
            "import pytest\npytestmark = pytest.mark.unit\n"
        )

        assert qa_metrics.discover_active_layers(tmp_path) == []


class TestCoveragePercent:
    def test_reads_coverage_json_total(self, tmp_path):
        coverage = tmp_path / "coverage.json"
        coverage.write_text(
            json.dumps({"totals": {"percent_covered": 69.4}}),
            encoding="utf-8",
        )

        assert qa_metrics.read_coverage_percent(coverage) == 69.4


class TestBadgeSvg:
    def test_badge_escapes_labels_and_values(self):
        svg = qa_metrics._badge("a<b", "c>d", "#000")

        assert "a&lt;b" in svg
        assert "c&gt;d" in svg
