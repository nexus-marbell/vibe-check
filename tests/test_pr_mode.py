"""Tests for PR mode: delta reports, URL parsing, argparse, and ref comparison."""

from pathlib import Path
from unittest.mock import patch
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from vibe_check import (
    DimensionResult,
    HotspotEntry,
    ReportData,
    _build_parser,
    _diff_reports,
    _direction_arrow,
    _parse_pr_url,
    _resolve_pr_refs,
    _run_analysis,
)

import pytest


# -- _direction_arrow ---------------------------------------------------------


class TestDirectionArrow:
    def test_positive(self):
        assert _direction_arrow(5.0) == "^"

    def test_negative(self):
        assert _direction_arrow(-3.0) == "v"

    def test_zero(self):
        assert _direction_arrow(0.0) == "="


# -- _parse_pr_url ------------------------------------------------------------


class TestParsePrUrl:
    def test_standard_url(self):
        owner, repo, num = _parse_pr_url(
            "https://github.com/acme/widgets/pull/42"
        )
        assert owner == "acme"
        assert repo == "widgets"
        assert num == 42

    def test_url_with_files_suffix(self):
        owner, repo, num = _parse_pr_url(
            "https://github.com/org/repo/pull/7/files"
        )
        assert owner == "org"
        assert repo == "repo"
        assert num == 7

    def test_http_url(self):
        owner, repo, num = _parse_pr_url(
            "http://github.com/org/repo/pull/99"
        )
        assert owner == "org"
        assert repo == "repo"
        assert num == 99

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            _parse_pr_url("https://gitlab.com/org/repo/merge_requests/1")

    def test_missing_pull_number_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            _parse_pr_url("https://github.com/org/repo")

    def test_not_a_url_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            _parse_pr_url("/some/local/path")


# -- _resolve_pr_refs (mocked gh CLI) ----------------------------------------


class TestResolvePrRefs:
    def test_success(self):
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"baseRefName":"main","headRefName":"feat/cool"}',
            stderr="",
        )
        with patch("vibe_check._run", return_value=mock_result):
            url, base, head, slug = _resolve_pr_refs(
                "https://github.com/acme/widgets/pull/10"
            )
        assert url == "https://github.com/acme/widgets"
        assert base == "main"
        assert head == "feat/cool"
        assert slug == "acme/widgets"

    def test_gh_not_found(self):
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=127, stdout="", stderr="Command not found: gh",
        )
        with patch("vibe_check._run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                _resolve_pr_refs(
                    "https://github.com/acme/widgets/pull/10"
                )

    def test_gh_failure(self):
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not found",
        )
        with patch("vibe_check._run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="gh pr view failed"):
                _resolve_pr_refs(
                    "https://github.com/acme/widgets/pull/10"
                )

    def test_bad_json(self):
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr="",
        )
        with patch("vibe_check._run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="Failed to parse"):
                _resolve_pr_refs(
                    "https://github.com/acme/widgets/pull/10"
                )


# -- _diff_reports ------------------------------------------------------------


def _make_report(
    name: str = "test-repo",
    sha: str = "abc123",
    grade: str = "B",
    score: float = 80.0,
    dims: list[DimensionResult] | None = None,
    risk_flags: list[str] | None = None,
    auto_f: list[str] | None = None,
    hotspots: list[HotspotEntry] | None = None,
) -> ReportData:
    """Build a ReportData with sensible defaults for testing."""
    return ReportData(
        repo_name=name,
        commit_sha=sha,
        overall_grade=grade,
        overall_score=score,
        dimensions=dims or [],
        risk_flags=risk_flags or [],
        auto_f_triggers=auto_f or [],
        hotspots=hotspots or [],
    )


class TestDiffReports:
    def test_overall_change_shown(self):
        base = _make_report(sha="aaa", grade="B", score=80)
        head = _make_report(sha="bbb", grade="C", score=65)
        result = _diff_reports(base, head)
        assert "B (80) -> C (65)" in result
        assert "-15" in result
        assert "v" in result

    def test_improvement_shown(self):
        base = _make_report(sha="aaa", grade="C", score=65)
        head = _make_report(sha="bbb", grade="B", score=80)
        result = _diff_reports(base, head)
        assert "+15" in result
        assert "^" in result

    def test_no_change(self):
        base = _make_report(sha="aaa", grade="B", score=80)
        head = _make_report(sha="bbb", grade="B", score=80)
        result = _diff_reports(base, head)
        assert "+0" in result
        assert "=" in result

    def test_dimension_deltas(self):
        base = _make_report(
            sha="aaa",
            dims=[DimensionResult("Linting", "0 issues", "A", 95)],
        )
        head = _make_report(
            sha="bbb",
            dims=[DimensionResult("Linting", "30 issues", "C", 62)],
        )
        result = _diff_reports(base, head)
        assert "Linting" in result
        assert "A (95)" in result
        assert "C (62)" in result
        assert "-33" in result

    def test_new_dimension_in_head(self):
        base = _make_report(sha="aaa", dims=[])
        head = _make_report(
            sha="bbb",
            dims=[DimensionResult("Type Safety", "0", "A", 100)],
        )
        result = _diff_reports(base, head)
        assert "Type Safety" in result
        assert "NEW" in result

    def test_removed_dimension(self):
        base = _make_report(
            sha="aaa",
            dims=[DimensionResult("Health", "80/100", "B", 80)],
        )
        head = _make_report(sha="bbb", dims=[])
        result = _diff_reports(base, head)
        assert "Health" in result
        assert "REMOVED" in result

    def test_new_risk_flags(self):
        base = _make_report(sha="aaa", risk_flags=["old flag"])
        head = _make_report(sha="bbb", risk_flags=["old flag", "new danger"])
        result = _diff_reports(base, head)
        assert "New Risk Flags" in result
        assert "new danger" in result
        assert "[NEW]" in result

    def test_resolved_risk_flags(self):
        base = _make_report(sha="aaa", risk_flags=["fixed thing"])
        head = _make_report(sha="bbb", risk_flags=[])
        result = _diff_reports(base, head)
        assert "Resolved Risk Flags" in result
        assert "fixed thing" in result
        assert "[RESOLVED]" in result

    def test_new_auto_f_triggers(self):
        base = _make_report(sha="aaa", auto_f=[])
        head = _make_report(sha="bbb", auto_f=["No license file (legal risk)"])
        result = _diff_reports(base, head)
        assert "New Auto-F Triggers" in result
        assert "No license" in result

    def test_new_hotspots(self):
        base = _make_report(sha="aaa", hotspots=[])
        head = _make_report(
            sha="bbb",
            hotspots=[HotspotEntry("src/foo.py", "process_data", 18, 0.0)],
        )
        result = _diff_reports(base, head)
        assert "New Complexity Hotspots" in result
        assert "foo.py:process_data CC=18" in result

    def test_resolved_hotspots(self):
        base = _make_report(
            sha="aaa",
            hotspots=[HotspotEntry("src/bar.py", "bad_func", 22, 0.0)],
        )
        head = _make_report(sha="bbb", hotspots=[])
        result = _diff_reports(base, head)
        assert "Resolved Complexity Hotspots" in result
        assert "bar.py:bad_func CC=22" in result


# -- _build_parser / argparse -------------------------------------------------


class TestArgparse:
    def test_full_repo_mode(self):
        parser = _build_parser()
        args = parser.parse_args(["https://github.com/org/repo"])
        assert args.target == "https://github.com/org/repo"
        assert args.pr is None
        assert args.compare is None

    def test_pr_mode(self):
        parser = _build_parser()
        args = parser.parse_args(
            ["--pr", "https://github.com/org/repo/pull/5"]
        )
        assert args.pr == "https://github.com/org/repo/pull/5"

    def test_compare_mode(self):
        parser = _build_parser()
        args = parser.parse_args(
            ["--compare", "main...feat", "https://github.com/org/repo"]
        )
        assert args.compare == "main...feat"
        assert args.target == "https://github.com/org/repo"

    def test_compare_local(self):
        parser = _build_parser()
        args = parser.parse_args(
            ["--compare", "main...feat", "/local/path"]
        )
        assert args.compare == "main...feat"
        assert args.target == "/local/path"


# -- _run_analysis (integration-level, mocked tools) -------------------------


class TestRunAnalysis:
    def test_returns_report_data(self, tmp_path):
        """Verify _run_analysis returns a populated ReportData."""
        # Create a minimal Python project
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        (tmp_path / "main.py").write_text("def hello():\n    return 'hi'\n")
        (tmp_path / "LICENSE").write_text("MIT License ...")
        (tmp_path / "README.md").write_text("x" * 200)
        (tmp_path / ".gitignore").write_text("__pycache__/")
        (tmp_path / "tests").mkdir()

        report = _run_analysis(tmp_path, "test-repo", "abc123", "2026-03-16")

        assert isinstance(report, ReportData)
        assert report.repo_name == "test-repo"
        assert report.commit_sha == "abc123"
        assert report.overall_grade in ("A", "B", "C", "D", "F", "?")
        assert len(report.dimensions) > 0
