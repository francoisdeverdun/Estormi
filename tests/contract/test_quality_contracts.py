"""Repository-level contracts for docs, tests, and CI pipelines."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _make_targets() -> set[str]:
    # Targets live in the root Makefile plus the thematic make/*.mk includes it
    # pulls in, so scan all of them (a target moved into an include must still
    # count as defined).
    text = _read("Makefile")
    for mk in sorted((ROOT / "make").glob("*.mk")):
        text += "\n" + mk.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s|$)", text, re.M))


class TestDocumentationContracts:
    def test_readme_links_dev_docs(self):
        """The README is the open-source landing page — it points visitors at
        the contributor docs (docs/)."""
        readme = _read("README.md")

        assert "docs/" in readme

    def test_testing_docs_match_makefile_targets(self):
        testing_doc = _read("docs/testing.md")
        makefile = _read("Makefile")
        targets = _make_targets()
        expected_targets = [
            "test",
            "test-unit",
            "test-integration",
            "test-e2e",
            "test-contract",
            "test-fast",
            "test-suite",
        ]

        missing_from_makefile = [target for target in expected_targets if target not in targets]
        missing_from_docs = [
            target for target in expected_targets if f"make {target}" not in testing_doc
        ]

        assert missing_from_makefile == []
        assert missing_from_docs == []
        assert "^[a-zA-Z0-9_-]+:.*?##" in makefile
        assert "unit+integration+e2e+contract" in testing_doc


class TestPipelineContracts:
    def test_github_qa_runs_docs_contracts_full_tests_runtime_and_security(self):
        qa = _read(".github/workflows/test.yml")
        security = _read(".github/workflows/security.yml")
        rust = _read(".github/workflows/rust.yml")

        for required in [
            "name: QA",
            "pull_request:",
            "branches: [main]",
            "permissions:\n  contents: read",
            "make test-unit",
            "make test-integration",
            "make test-e2e",
            "make test-contract",
            # The SPA Playwright e2e is the only browser-level coverage of the
            # load-bearing flows; pin its CI step so it can't be silently dropped.
            "make test-e2e-frontend",
            "pytest tests/ --tb=short -q --cov=estormi_server --cov=memory_core --cov=connectors --cov=estormi_ingestion --cov=estormi_briefing --cov=estormi_distill",
            # Pin the concrete floor so it can't be silently weakened (e.g. to =10).
            "--cov-fail-under=80",
            "--junitxml=test-results.xml",
            "scripts/qa_metrics.py build/coverage/coverage.json assets/badges",
            "qa-metrics.json",
            "make test-suite",
        ]:
            assert required in qa, f"missing in test.yml: {required}"

        for required in [
            "scripts/security_scan.py --include-untracked",
            # The git-history authorship gate (the public-release PII blocker)
            # must stay wired into CI — pin it so it can't be silently dropped.
            "scripts/security_scan.py --history",
            "scripts/detect_secrets_gate.py",
            "bandit -r packages scripts -lll",
            "pip-audit -r packages/estormi_server/requirements.txt",
            "trufflehog",
        ]:
            assert required in security, f"missing in security.yml: {required}"

        for required in [
            "cargo fmt --check",
            "cargo clippy --locked --all-targets -- -D warnings",
            "cargo audit",
        ]:
            assert required in rust, f"missing in rust.yml: {required}"

        # Nightly security scan keeps the bandit/pip-audit/trufflehog signal
        # off the PR critical path.
        assert "schedule:" in security

        for forbidden in [
            "make tag",
            "git tag",
            "gh release create",
            "softprops/action-gh-release",
        ]:
            assert forbidden not in qa

    def test_github_release_is_tag_only_and_tests_before_building(self):
        workflow = _read(".github/workflows/release.yml")

        assert "tags:" in workflow
        assert "- 'v*'" in workflow
        assert "needs: [lint, test]" in workflow
        assert "cargo tauri build" in workflow
        assert "softprops/action-gh-release" in workflow
        assert "pull_request:" not in workflow
        assert "branches: [main]" not in workflow
        assert "make tag" not in workflow

    def test_ruff_version_is_consistent_across_local_and_ci_config(self):
        pre_commit = _read(".pre-commit-config.yaml")
        match = re.search(r"rev:\s+v([0-9.]+)", pre_commit)

        assert match is not None
        expected = match.group(1)
        for relative in [
            ".github/workflows/test.yml",
            ".github/workflows/release.yml",
        ]:
            assert f"ruff=={expected}" in _read(relative)
        # The local gate must run the SAME ruff. With CI billing off, `make lint`
        # is the sole gate: tests/requirements-test.txt pins .venv's ruff and
        # make/quality.mk enforces RUFF_VERSION before running it. Both must
        # equal the pre-commit/CI pin, else the gate diverges from CI silently.
        assert f"ruff=={expected}" in _read("tests/requirements-test.txt")
        assert f"RUFF_VERSION := {expected}" in _read("Makefile")

    def test_github_workflows_use_node24_ready_official_actions(self):
        combined = "\n".join(
            _read(relative)
            for relative in [
                ".github/workflows/test.yml",
                ".github/workflows/release.yml",
                ".github/workflows/rust.yml",
                ".github/workflows/js.yml",
                ".github/workflows/security.yml",
            ]
        )

        for required in [
            "actions/checkout@v6",
            "actions/setup-python@v6",
            "actions/upload-artifact@v7",
            "actions/cache@v5",
        ]:
            assert required in combined

        for deprecated in [
            "actions/checkout@v4",
            "actions/setup-python@v5",
            "actions/upload-artifact@v4",
            "actions/cache@v4",
        ]:
            assert deprecated not in combined
