"""Tests for vibe-check grading logic."""

import json
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
    _summarize_pyscn_clones,
    stage_duplication,
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


class TestPyscnDuplicationStage:
    """Issue #16 — duplication signal sourced from pyscn JSON, not deepcsim."""

    def _write_pyscn_json(self, repo: Path, payload: dict) -> Path:
        reports_dir = repo / ".pyscn" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / "analyze_test.json"
        path.write_text(json.dumps(payload))
        return path

    def test_summarize_clones_groups_by_file(self):
        clones = [
            {"location": {"file_path": "src/a.py"}},
            {"location": {"file_path": "src/a.py"}},
            {"location": {"file_path": "src/b.py"}},
        ]
        lines = _summarize_pyscn_clones(clones)
        # Most-frequent file ranked first, line format "<name>: N clone fragment(s)".
        assert lines[0].startswith("- a.py:")
        assert "2 clone fragments" in lines[0]
        assert "1 clone fragment" in lines[1]
        assert "fragments" not in lines[1]  # singular for n=1

    def test_summarize_clones_handles_missing_locations(self):
        clones = [{"size": 10}, {"location": None}, {"location": {"file_path": ""}}]
        assert _summarize_pyscn_clones(clones) == []

    def test_stage_duplication_reads_pyscn_json(self, tmp_path):
        self._write_pyscn_json(tmp_path, {
            "summary": {
                "duplication_score": 100,
                "clone_groups": 0,
                "code_duplication_percentage": 0,
            },
            "clone": {"clones": []},
        })
        report = ReportData()
        stage_duplication(tmp_path, report)
        dim = next(d for d in report.dimensions if d.name == "Duplication")
        assert dim.score == 100.0
        assert dim.grade == "A"
        assert "0 clone groups" in dim.raw_value
        assert "(0.0% duplication)" in dim.raw_value
        assert report.tool_errors == []
        assert report.risk_flags == []

    def test_stage_duplication_flags_high_clone_count(self, tmp_path):
        self._write_pyscn_json(tmp_path, {
            "summary": {
                "duplication_score": 30,
                "clone_groups": 12,
                "code_duplication_percentage": 18.5,
            },
            "clone": {"clones": [{"location": {"file_path": "x.py"}}]},
        })
        report = ReportData()
        stage_duplication(tmp_path, report)
        dim = next(d for d in report.dimensions if d.name == "Duplication")
        assert dim.score == 30.0
        assert dim.grade == "F"
        assert "12 clone groups" in dim.raw_value
        # Risk flag fires when clone_groups > 5.
        assert any("12 clone groups" in flag for flag in report.risk_flags)

    def test_stage_duplication_no_json_distinguishes_from_real_zero(self, tmp_path):
        # No pyscn report present — unparsed, neutral 50, no Grade-F risk flag.
        report = ReportData()
        stage_duplication(tmp_path, report)
        dim = next(d for d in report.dimensions if d.name == "Duplication")
        assert dim.score == 50
        assert dim.grade == "?"
        assert dim.raw_value == "unparsed"
        assert any("no JSON report" in err for err in report.tool_errors)
        assert report.risk_flags == []

    def test_stage_duplication_malformed_json(self, tmp_path):
        reports_dir = tmp_path / ".pyscn" / "reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "analyze_test.json").write_text("not json")
        report = ReportData()
        stage_duplication(tmp_path, report)
        dim = next(d for d in report.dimensions if d.name == "Duplication")
        assert dim.score == 50
        assert dim.grade == "?"
        assert any("malformed JSON" in err for err in report.tool_errors)

    def test_stage_duplication_missing_score_field(self, tmp_path):
        self._write_pyscn_json(tmp_path, {"summary": {"clone_groups": 0}})
        report = ReportData()
        stage_duplication(tmp_path, report)
        dim = next(d for d in report.dimensions if d.name == "Duplication")
        assert dim.score == 50
        assert dim.grade == "?"
        assert any("duplication_score missing" in err for err in report.tool_errors)
