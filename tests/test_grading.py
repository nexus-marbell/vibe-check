"""Tests for vibe-check grading logic."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from vibe_check import (
    DimensionResult,
    ReportData,
    _score_to_grade,
    _score_cc,
    _score_mi,
    compute_overall,
    _check_has_readme,
    _check_has_tests,
    detect_languages,
    _parse_pyscn_text,
    _parse_pyscn_json,
    _find_pyscn_json,
)


class TestScoreToGrade:
    def test_grade_a(self):
        assert _score_to_grade(95) == "A"
        assert _score_to_grade(90) == "A"

    def test_grade_b(self):
        assert _score_to_grade(89) == "B"
        assert _score_to_grade(75) == "B"

    def test_grade_c(self):
        assert _score_to_grade(74) == "C"
        assert _score_to_grade(60) == "C"

    def test_grade_d(self):
        assert _score_to_grade(59) == "D"
        assert _score_to_grade(40) == "D"

    def test_grade_f(self):
        assert _score_to_grade(39) == "F"
        assert _score_to_grade(0) == "F"


class TestScoreCC:
    def test_excellent(self):
        assert _score_cc(2.0, 8) == 95

    def test_good(self):
        assert _score_cc(4.0, 12) == 80

    def test_moderate(self):
        assert _score_cc(7.0, 18) == 65

    def test_poor(self):
        assert _score_cc(11.0, 25) == 45

    def test_terrible(self):
        assert _score_cc(20.0, 60) == 25


class TestScoreMI:
    def test_no_data_neutral(self):
        assert _score_mi(0, False) == 50

    def test_excellent(self):
        assert _score_mi(45.0, True) == 95

    def test_poor(self):
        assert _score_mi(5.0, True) == 20


class TestAutoFTriggers:
    def test_auto_f_overrides_grade(self):
        report = ReportData()
        report.dimensions.append(DimensionResult("Linting", "0", "A", 100))
        report.dimensions.append(DimensionResult("Health", "90", "A", 90))
        report.dimensions.append(DimensionResult("Complexity", "ok", "A", 90))
        report.auto_f_triggers.append("No license file (legal risk)")
        compute_overall(report)
        assert report.overall_grade == "F"
        assert report.overall_score <= 39

    def test_no_triggers_normal_grade(self):
        report = ReportData()
        report.dimensions.append(DimensionResult("Linting", "0", "A", 100))
        report.dimensions.append(DimensionResult("Health", "90", "A", 90))
        report.dimensions.append(DimensionResult("Complexity", "ok", "A", 90))
        report.dimensions.append(DimensionResult("Type Safety", "0", "A", 100))
        report.dimensions.append(DimensionResult("Duplication", "0%", "A", 100))
        report.dimensions.append(DimensionResult("Hygiene", "ok", "A", 90))
        compute_overall(report)
        assert report.overall_grade in ("A", "B")
        assert report.overall_score >= 75


class TestLanguageDetection:
    def test_detects_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")
        langs = detect_languages(tmp_path)
        assert "python" in langs

    def test_detects_typescript(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        langs = detect_languages(tmp_path)
        assert "typescript" in langs

    def test_detects_both(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")
        (tmp_path / "tsconfig.json").write_text("{}")
        langs = detect_languages(tmp_path)
        assert "python" in langs
        assert "typescript" in langs

    def test_detects_py_files(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hi')")
        langs = detect_languages(tmp_path)
        assert "python" in langs


class TestHygieneHelpers:
    def test_has_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("x" * 200)
        assert _check_has_readme(tmp_path)

    def test_no_readme(self, tmp_path):
        assert not _check_has_readme(tmp_path)

    def test_trivial_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("short")
        assert not _check_has_readme(tmp_path)

    def test_has_tests_dir(self, tmp_path):
        (tmp_path / "tests").mkdir()
        assert _check_has_tests(tmp_path)

    def test_has_test_files(self, tmp_path):
        (tmp_path / "test_foo.py").write_text("pass")
        assert _check_has_tests(tmp_path)

    def test_no_tests(self, tmp_path):
        assert not _check_has_tests(tmp_path)


class TestPyscnHealthParser:
    """Issue #17 — distinguish parser-failed from real-zero, prefer JSON."""

    def test_text_parser_extracts_score(self):
        text = "Health Score: 84/100 (Grade: B)"
        assert _parse_pyscn_text(text) == 84.0

    def test_text_parser_returns_none_on_no_match(self):
        # Was 0.0 before #17 fix — silent fallback masqueraded as a real F.
        assert _parse_pyscn_text("no health line here") is None

    def test_text_parser_extracts_real_zero(self):
        # A real 0/100 score IS a valid parse — distinguish from no-match (None).
        text = "Health Score: 0/100 (Grade: F)"
        assert _parse_pyscn_text(text) == 0.0

    def test_json_parser_reads_summary_health_score(self, tmp_path):
        report = tmp_path / "analyze_test.json"
        report.write_text('{"summary": {"health_score": 61, "grade": "C"}}')
        assert _parse_pyscn_json(report) == 61.0

    def test_json_parser_handles_missing_field(self, tmp_path):
        report = tmp_path / "analyze_test.json"
        report.write_text('{"summary": {}}')
        assert _parse_pyscn_json(report) is None

    def test_json_parser_handles_malformed_json(self, tmp_path):
        report = tmp_path / "analyze_test.json"
        report.write_text("not json")
        assert _parse_pyscn_json(report) is None

    def test_json_parser_handles_missing_file(self, tmp_path):
        assert _parse_pyscn_json(tmp_path / "nonexistent.json") is None

    def test_find_json_from_stdout_path(self, tmp_path):
        report_path = tmp_path / "analyze_20260504.json"
        report_path.write_text('{"summary": {"health_score": 75}}')
        stdout = f"Unified JSON report generated: {report_path}\nHealth Score: 75/100"
        found = _find_pyscn_json(stdout, tmp_path)
        assert found == report_path

    def test_find_json_falls_back_to_glob(self, tmp_path):
        reports_dir = tmp_path / ".pyscn" / "reports"
        reports_dir.mkdir(parents=True)
        older = reports_dir / "analyze_20260501.json"
        older.write_text("{}")
        newer = reports_dir / "analyze_20260504.json"
        newer.write_text("{}")
        # bump newer's mtime explicitly so glob sort is deterministic
        import os as _os
        import time as _time
        _os.utime(newer, (_time.time(), _time.time()))
        _os.utime(older, (_time.time() - 100, _time.time() - 100))
        # No path in stdout — should fall back to newest in reports dir.
        found = _find_pyscn_json("", tmp_path)
        assert found == newer

    def test_find_json_returns_none_when_no_reports(self, tmp_path):
        assert _find_pyscn_json("", tmp_path) is None
