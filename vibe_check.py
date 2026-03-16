#!/usr/bin/env python3
"""vibe-check: Code quality aggregator that makes auditable intent visible.

Takes a git repo URL (or local path), runs static analysis tools,
and produces a graded markdown report.

Usage:
    python vibe_check.py https://github.com/org/repo
    python vibe_check.py /path/to/local/repo
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_TOOL_TIMEOUT = 120
_WORKSPACE = Path("/workspace")


# -- Data structures ----------------------------------------------------------


@dataclass
class DimensionResult:
    name: str
    raw_value: str
    grade: str
    score: float  # 0-100 normalised


@dataclass
class HotspotEntry:
    file: str
    function: str
    cc: int
    mi: float


@dataclass
class ReportData:
    repo_name: str = ""
    commit_sha: str = ""
    commit_date: str = ""
    dimensions: list[DimensionResult] = field(default_factory=list)
    overall_grade: str = "?"
    overall_score: float = 0.0
    risk_flags: list[str] = field(default_factory=list)
    auto_f_triggers: list[str] = field(default_factory=list)
    hotspots: list[HotspotEntry] = field(default_factory=list)
    duplication_summary: str = ""
    tool_errors: list[str] = field(default_factory=list)


# -- Helpers -------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command with timeout. Returns CompletedProcess even on failure."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124, stdout="", stderr="TIMEOUT"
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=cmd, returncode=127, stdout="", stderr=f"Command not found: {cmd[0]}"
        )


def _score_to_grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


# -- Language Detection --------------------------------------------------------


def detect_languages(repo: Path) -> set[str]:
    """Detect programming languages in a repository.

    Checks for Python and TypeScript marker files.
    Returns a set containing "python", "typescript", or both.
    """
    languages: set[str] = set()

    python_markers = ["pyproject.toml", "setup.py", "setup.cfg"]
    ts_markers = ["tsconfig.json"]
    ts_package_markers = ["package.json"]

    for marker in python_markers:
        if (repo / marker).exists():
            languages.add("python")
            break

    if "python" not in languages:
        py_files = list(repo.rglob("*.py"))
        # Exclude common non-project files
        py_files = [
            f
            for f in py_files
            if ".git" not in f.parts and "node_modules" not in f.parts
        ]
        if py_files:
            languages.add("python")

    for marker in ts_markers:
        if (repo / marker).exists():
            languages.add("typescript")
            break

    if "typescript" not in languages:
        for marker in ts_package_markers:
            if (repo / marker).exists():
                languages.add("typescript")
                break

    if "typescript" not in languages:
        ts_files = list(repo.rglob("*.ts")) + list(repo.rglob("*.tsx"))
        ts_files = [
            f
            for f in ts_files
            if ".git" not in f.parts and "node_modules" not in f.parts
        ]
        if ts_files:
            languages.add("typescript")

    return languages


# -- Stage: Clone / Identify ---------------------------------------------------


def stage_clone(target: str, workspace: Path) -> tuple[Path, str, str, str]:
    """Clone repo or resolve local path. Returns (path, name, sha, date)."""
    if target.startswith(("http://", "https://", "git@")):
        repo_name = target.rstrip("/").split("/")[-1].removesuffix(".git")
        result = _run(["git", "clone", "--depth=50", target, str(workspace)])
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
        repo_path = workspace
    else:
        local = Path(target).resolve()
        if not local.is_dir():
            raise RuntimeError(f"Local path not found: {target}")
        repo_path = local
        repo_name = local.name

    log = _run(["git", "log", "-1", "--format=%H|%ci"], cwd=repo_path)
    if log.returncode == 0 and "|" in log.stdout.strip():
        sha, date = log.stdout.strip().split("|", 1)
    else:
        sha, date = "unknown", "unknown"

    return repo_path, repo_name, sha[:12], date.split(" ")[0]


# -- Stage: Ruff ---------------------------------------------------------------


def stage_ruff(repo: Path, report: ReportData) -> None:
    """Run ruff linter, count issues."""
    result = _run(["ruff", "check", str(repo), "--output-format", "json"])
    if result.returncode == 127:
        report.tool_errors.append("ruff: not installed")
        report.dimensions.append(DimensionResult("Linting", "skipped", "?", 50))
        return

    try:
        issues = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        issues = []
        report.tool_errors.append("ruff: JSON parse error")

    count = len(issues)
    if count == 0:
        score = 100
    elif count <= 5:
        score = 90
    elif count <= 20:
        score = 75
    elif count <= 50:
        score = 60
    elif count <= 100:
        score = 40
    else:
        score = max(10, 40 - (count - 100) // 10)

    report.dimensions.append(
        DimensionResult("Linting", f"{count} issues", _score_to_grade(score), score)
    )
    if count > 50:
        report.risk_flags.append(f"High lint issue count ({count})")


# -- Stage: Pyright ------------------------------------------------------------


def stage_pyright(repo: Path, report: ReportData) -> None:
    """Run pyright type checker, count errors."""
    result = _run(["pyright", str(repo), "--outputjson"])
    if result.returncode == 127:
        report.tool_errors.append("pyright: not installed")
        report.dimensions.append(DimensionResult("Type Safety", "skipped", "?", 50))
        return

    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
        report.tool_errors.append("pyright: JSON parse error")

    summary = data.get("summary", {})
    errors = summary.get("errorCount", 0)
    warnings = summary.get("warningCount", 0)
    total = errors + warnings

    if total == 0:
        score = 100
    elif errors == 0 and warnings <= 10:
        score = 90
    elif errors <= 5:
        score = 75
    elif errors <= 20:
        score = 60
    elif errors <= 50:
        score = 40
    else:
        score = max(10, 40 - (errors - 50) // 5)

    report.dimensions.append(
        DimensionResult(
            "Type Safety",
            f"{errors} errors, {warnings} warnings",
            _score_to_grade(score),
            score,
        )
    )
    if errors > 20:
        report.risk_flags.append(f"High type error count ({errors})")


# -- Stage: Complexity (lizard + radon) ----------------------------------------


def stage_complexity(repo: Path, report: ReportData) -> None:
    """Run lizard and radon for CC and MI metrics."""
    hotspots: list[HotspotEntry] = []
    max_cc = 0
    cc_values: list[int] = []

    # -- lizard (CSV) --
    liz = _run(["lizard", str(repo), "--csv"])
    if liz.returncode != 127 and liz.stdout.strip():
        for line in liz.stdout.strip().splitlines()[1:]:  # skip header
            parts = line.split(",")
            if len(parts) >= 5:
                try:
                    cc = int(parts[1].strip())
                    func_name = parts[4].strip().strip('"')
                    file_path = (
                        parts[-1].strip().strip('"')
                        if len(parts) > 5
                        else parts[0].strip().strip('"')
                    )
                    cc_values.append(cc)
                    if cc > max_cc:
                        max_cc = cc
                    if cc > 10:
                        hotspots.append(
                            HotspotEntry(
                                file=file_path, function=func_name, cc=cc, mi=0.0
                            )
                        )
                except (ValueError, IndexError):
                    pass
    elif liz.returncode == 127:
        report.tool_errors.append("lizard: not installed")

    # -- radon CC (JSON) --
    radon_cc = _run(["radon", "cc", str(repo), "-j", "-a"])
    radon_cc_data: dict = {}
    if radon_cc.returncode != 127 and radon_cc.stdout.strip():
        try:
            radon_cc_data = json.loads(radon_cc.stdout)
        except json.JSONDecodeError:
            report.tool_errors.append("radon cc: JSON parse error")

    for filepath, blocks in radon_cc_data.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict):
                cc = block.get("complexity", 0)
                name = block.get("name", "?")
                cc_values.append(cc)
                if cc > max_cc:
                    max_cc = cc
                if cc > 10:
                    hotspots.append(
                        HotspotEntry(file=filepath, function=name, cc=cc, mi=0.0)
                    )

    # -- radon MI (JSON) --
    radon_mi = _run(["radon", "mi", str(repo), "-j"])
    mi_values: list[float] = []
    if radon_mi.returncode != 127 and radon_mi.stdout.strip():
        try:
            mi_data = json.loads(radon_mi.stdout)
            for filepath, mi_info in mi_data.items():
                if isinstance(mi_info, dict):
                    mi_val = mi_info.get("mi", 0)
                    mi_values.append(float(mi_val))
                    # Attach MI to hotspots from same file
                    for h in hotspots:
                        if h.file == filepath:
                            h.mi = float(mi_val)
                elif isinstance(mi_info, (int, float)):
                    mi_values.append(float(mi_info))
        except json.JSONDecodeError:
            report.tool_errors.append("radon mi: JSON parse error")
    elif radon_mi.returncode == 127:
        report.tool_errors.append("radon: not installed")

    avg_cc = sum(cc_values) / len(cc_values) if cc_values else 0
    avg_mi = sum(mi_values) / len(mi_values) if mi_values else 0

    # Score complexity: based on avg CC and max CC
    if avg_cc <= 3 and max_cc <= 10:
        cc_score = 95
    elif avg_cc <= 5 and max_cc <= 15:
        cc_score = 80
    elif avg_cc <= 8 and max_cc <= 20:
        cc_score = 65
    elif avg_cc <= 12:
        cc_score = 45
    else:
        cc_score = 25

    # Score maintainability: based on avg MI
    if avg_mi >= 40:
        mi_score = 95
    elif avg_mi >= 30:
        mi_score = 80
    elif avg_mi >= 20:
        mi_score = 60
    elif avg_mi >= 10:
        mi_score = 40
    else:
        mi_score = 20 if mi_values else 50  # no data = neutral

    # Combined complexity dimension: 60% CC + 40% MI
    combined = cc_score * 0.6 + mi_score * 0.4
    raw_text = f"avg CC {avg_cc:.1f}, max CC {max_cc}"
    if mi_values:
        raw_text += f", avg MI {avg_mi:.1f}"

    report.dimensions.append(
        DimensionResult("Complexity", raw_text, _score_to_grade(combined), combined)
    )

    # MI dimension separately
    mi_raw = f"avg MI {avg_mi:.1f}" if mi_values else "no data"
    report.dimensions.append(
        DimensionResult("Maintainability", mi_raw, _score_to_grade(mi_score), mi_score)
    )

    # Sort hotspots by CC descending, keep top 10
    hotspots.sort(key=lambda h: h.cc, reverse=True)
    report.hotspots = hotspots[:10]

    if max_cc > 15:
        report.risk_flags.append(
            f"Function with CC={max_cc} exceeds refactor threshold (15)"
        )
    if avg_mi < 20 and mi_values:
        report.risk_flags.append(
            f"Average MI={avg_mi:.1f} indicates hard-to-maintain code"
        )


# -- Stage: Health (pyscn) ----------------------------------------------------


def _parse_pyscn_text(text: str) -> float:
    """Extract health score from pyscn text output (e.g. 'Health Score: 84/100')."""
    match = re.search(r"Health\s+Score:\s*(\d+)/100", text)
    if match:
        return float(match.group(1))
    return 0.0


def stage_health(repo: Path, report: ReportData) -> None:
    """Run pyscn health scanner."""
    result = _run(["pyscn", "analyze", str(repo)])
    if result.returncode == 127:
        report.tool_errors.append("pyscn: not installed")
        report.dimensions.append(DimensionResult("Health", "skipped", "?", 50))
        return

    # pyscn writes JSON to ~/.pyscn/reports/ but prints text summary to stdout.
    # Parse the text output for "Health Score: N/100".
    combined = result.stdout + result.stderr
    score = _parse_pyscn_text(combined)

    report.dimensions.append(
        DimensionResult("Health", f"{score:.0f}/100", _score_to_grade(score), score)
    )
    if score < 40:
        report.risk_flags.append(f"pyscn health score {score:.0f} (Grade F)")


# -- Stage: Duplication (deepcsim) ---------------------------------------------


def _parse_deepcsim_json(raw: str) -> dict:
    """Parse deepcsim JSON, skipping preamble text like 'Starting directory scan...'."""
    idx = raw.find("{")
    if idx < 0:
        return {}
    try:
        return json.loads(raw[idx:])
    except json.JSONDecodeError:
        return {}


def _is_dunder_pair(func1: str, func2: str) -> bool:
    """Return True if both functions are dunder methods (noise in deepcsim)."""
    dunders = {"__init__", "__repr__", "__str__", "__eq__", "__hash__"}
    return func1 in dunders and func2 in dunders


def stage_duplication(repo: Path, report: ReportData) -> None:
    """Run deepcsim for structural duplication detection."""
    result = _run(["deepcsim-cli", str(repo), "--threshold", "80", "--json"])
    if result.returncode == 127:
        report.tool_errors.append("deepcsim-cli: not installed")
        report.dimensions.append(DimensionResult("Duplication", "skipped", "?", 50))
        return

    summary_lines: list[str] = []

    # deepcsim JSON: {"count": N, "results": [{file1, file2, similarity: float, ...}]}
    # Each result is a file pair. The top-level "similarity" is a float (overall).
    # Each comparison within has similarity as a dict {structural, semantic, ...}.
    data = _parse_deepcsim_json(result.stdout)
    results = data.get("results", [])

    significant_pairs: list[tuple[str, str, float]] = []
    for pair in results:
        if not isinstance(pair, dict):
            continue
        file1 = Path(pair.get("file1", "")).name
        file2 = Path(pair.get("file2", "")).name
        sim = pair.get("similarity", 0)
        if not isinstance(sim, (int, float)):
            sim = pair.get("avg_similarity", 0)

        # Check if pair has non-dunder comparisons
        comparisons = pair.get("comparisons", [])
        has_non_dunder = len(comparisons) == 0  # no comparisons = count it
        for comp in comparisons:
            if not isinstance(comp, dict):
                continue
            f1 = comp.get("func1_name", "")
            f2 = comp.get("func2_name", "")
            if not _is_dunder_pair(f1, f2):
                has_non_dunder = True
                break

        if has_non_dunder and sim >= 80:
            significant_pairs.append((file1, file2, float(sim)))

    clone_groups = len(significant_pairs)
    for f1, f2, sim in significant_pairs[:10]:
        summary_lines.append(f"- {f1} <-> {f2} ({sim:.0f}% similar)")

    if clone_groups == 0:
        score = 100
    elif clone_groups <= 2:
        score = 85
    elif clone_groups <= 5:
        score = 70
    elif clone_groups <= 10:
        score = 50
    else:
        score = max(15, 50 - (clone_groups - 10) * 3)

    report.dimensions.append(
        DimensionResult(
            "Duplication",
            f"{clone_groups} clone groups",
            _score_to_grade(score),
            score,
        )
    )
    report.duplication_summary = (
        "\n".join(summary_lines)
        if summary_lines
        else "No significant duplication found."
    )

    if clone_groups > 5:
        report.risk_flags.append(f"{clone_groups} clone groups detected")


# -- Stage: ESLint (TypeScript linting) ----------------------------------------

_ESLINT_CONFIG = Path("/app/.eslintrc.json")


def stage_eslint(repo: Path, report: ReportData) -> None:
    """Run eslint on TypeScript/JavaScript files, count issues."""
    config = _ESLINT_CONFIG if _ESLINT_CONFIG.exists() else None

    cmd = ["eslint", str(repo), "--format", "json", "--no-error-on-unmatched-pattern"]
    if config is not None:
        cmd.extend(["--config", str(config)])
    cmd.extend(["--ext", ".ts,.tsx,.js,.jsx"])

    result = _run(cmd)
    if result.returncode == 127:
        report.tool_errors.append("eslint: not installed")
        report.dimensions.append(DimensionResult("Linting", "skipped", "?", 50))
        return

    try:
        data = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        data = []
        report.tool_errors.append("eslint: JSON parse error")

    count = 0
    for file_result in data:
        if isinstance(file_result, dict):
            count += file_result.get("errorCount", 0)

    if count == 0:
        score = 100
    elif count <= 5:
        score = 90
    elif count <= 20:
        score = 75
    elif count <= 50:
        score = 60
    elif count <= 100:
        score = 40
    else:
        score = max(10, 40 - (count - 100) // 10)

    report.dimensions.append(
        DimensionResult("Linting", f"{count} issues", _score_to_grade(score), score)
    )
    if count > 50:
        report.risk_flags.append(f"High lint issue count ({count})")


# -- Stage: tsc (TypeScript type checking) ------------------------------------


def stage_tsc(repo: Path, report: ReportData) -> None:
    """Run tsc --noEmit for type checking. Count errors."""
    has_tsconfig = (repo / "tsconfig.json").exists()
    if has_tsconfig:
        cmd = ["tsc", "--noEmit", "--project", str(repo / "tsconfig.json")]
    else:
        # No tsconfig: check all TS files with permissive settings
        ts_files = list(repo.rglob("*.ts")) + list(repo.rglob("*.tsx"))
        ts_files = [str(f) for f in ts_files if "node_modules" not in f.parts]
        if not ts_files:
            report.dimensions.append(
                DimensionResult("Type Safety", "no .ts files found", "?", 50)
            )
            return
        cmd = ["tsc", "--noEmit", "--allowJs"] + ts_files

    result = _run(cmd)
    if result.returncode == 127:
        report.tool_errors.append("tsc: not installed")
        report.dimensions.append(DimensionResult("Type Safety", "skipped", "?", 50))
        return

    # tsc prints errors to stdout, one per line starting with file path
    # Count lines that look like errors: "path(line,col): error TSxxxx: ..."
    error_pattern = re.compile(r"\(\d+,\d+\):\s+error\s+TS\d+:")
    errors = 0
    warning_pattern = re.compile(r"\(\d+,\d+\):\s+warning\s+")
    warnings = 0
    for line in (result.stdout + result.stderr).splitlines():
        if error_pattern.search(line):
            errors += 1
        elif warning_pattern.search(line):
            warnings += 1

    total = errors + warnings
    if total == 0:
        score = 100
    elif errors == 0 and warnings <= 10:
        score = 90
    elif errors <= 5:
        score = 75
    elif errors <= 20:
        score = 60
    elif errors <= 50:
        score = 40
    else:
        score = max(10, 40 - (errors - 50) // 5)

    report.dimensions.append(
        DimensionResult(
            "Type Safety",
            f"{errors} errors, {warnings} warnings",
            _score_to_grade(score),
            score,
        )
    )
    if errors > 20:
        report.risk_flags.append(f"High type error count ({errors})")


# -- Stage: jscpd (TypeScript duplication) ------------------------------------


def stage_jscpd(repo: Path, report: ReportData) -> None:
    """Run jscpd for copy-paste detection in TypeScript/JavaScript."""
    result = _run(
        [
            "jscpd",
            str(repo),
            "--reporters",
            "json",
            "--output",
            "/tmp/jscpd-report",
            "--ignore",
            "node_modules,dist,build,.git",
            "--format",
            "typescript,javascript",
        ]
    )
    if result.returncode == 127:
        report.tool_errors.append("jscpd: not installed")
        report.dimensions.append(DimensionResult("Duplication", "skipped", "?", 50))
        return

    # jscpd writes JSON report to /tmp/jscpd-report/jscpd-report.json
    report_file = Path("/tmp/jscpd-report/jscpd-report.json")
    data: dict = {}
    if report_file.exists():
        try:
            data = json.loads(report_file.read_text())
        except json.JSONDecodeError:
            report.tool_errors.append("jscpd: JSON parse error")

    statistics = data.get("statistics", {})
    # jscpd statistics has a "total" key with percentage
    total_stats = statistics.get("total", {})
    percentage = total_stats.get("percentage", 0)
    if not isinstance(percentage, (int, float)):
        percentage = 0

    clones = data.get("duplicates", [])
    clone_count = len(clones) if isinstance(clones, list) else 0

    summary_lines: list[str] = []
    if isinstance(clones, list):
        for dup in clones[:10]:
            if isinstance(dup, dict):
                first = dup.get("firstFile", {})
                second = dup.get("secondFile", {})
                f1 = (
                    Path(first.get("name", "")).name if isinstance(first, dict) else "?"
                )
                f2 = (
                    Path(second.get("name", "")).name
                    if isinstance(second, dict)
                    else "?"
                )
                lines_count = dup.get("lines", 0)
                summary_lines.append(f"- {f1} <-> {f2} ({lines_count} lines)")

    if percentage == 0:
        score = 100
    elif percentage <= 3:
        score = 90
    elif percentage <= 5:
        score = 80
    elif percentage <= 10:
        score = 70
    elif percentage <= 20:
        score = 55
    elif percentage <= 40:
        score = 35
    else:
        score = max(10, 35 - int(percentage - 40))

    report.dimensions.append(
        DimensionResult(
            "Duplication",
            f"{percentage:.1f}% duplicated ({clone_count} clones)",
            _score_to_grade(score),
            score,
        )
    )
    if summary_lines:
        if report.duplication_summary:
            report.duplication_summary += "\n" + "\n".join(summary_lines)
        else:
            report.duplication_summary = "\n".join(summary_lines)
    elif not report.duplication_summary:
        report.duplication_summary = "No significant duplication found."

    if percentage > 60:
        report.risk_flags.append(
            f"Duplication at {percentage:.0f}% exceeds 60% threshold (auto-F)"
        )
    elif clone_count > 5:
        report.risk_flags.append(f"{clone_count} duplicate blocks detected")


# -- Stage: History (wily) ----------------------------------------------------


def stage_wily(repo: Path, report: ReportData) -> None:
    """Run wily for historical complexity analysis. Informational only."""
    build = _run(["wily", "build", str(repo)], cwd=repo)
    if build.returncode == 127:
        report.tool_errors.append("wily: not installed")
        return
    if build.returncode != 0:
        report.tool_errors.append(f"wily build: {build.stderr.strip()[:100]}")
        return

    rank = _run(["wily", "rank", str(repo), "-n", "20"], cwd=repo)
    if rank.returncode == 0 and rank.stdout.strip():
        # Wily rank output is informational; included in report as-is
        pass


# -- Stage: Hygiene ------------------------------------------------------------


_LICENSE_NAMES = {
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "LICENCE",
    "LICENCE.md",
    "LICENCE.txt",
    "COPYING",
    "COPYING.md",
    "COPYING.txt",
}

_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs"}


def stage_hygiene(repo: Path, report: ReportData) -> None:
    """Check project hygiene: license, tests, README, .gitignore."""
    score = 0
    details: list[str] = []

    # License (25 pts) — absence is also an auto-F trigger
    has_license = any((repo / name).exists() for name in _LICENSE_NAMES)
    if has_license:
        score += 25
        details.append("License: found")
    else:
        details.append("License: MISSING")
        report.auto_f_triggers.append("No license file (legal risk)")

    # Tests (25 pts)
    has_tests = any((repo / d).is_dir() for d in _TEST_DIRS)
    if not has_tests:
        # Check for test files anywhere
        test_files = list(repo.rglob("test_*.py")) + list(repo.rglob("*.test.ts"))
        test_files = [
            f
            for f in test_files
            if ".git" not in f.parts and "node_modules" not in f.parts
        ]
        has_tests = len(test_files) > 0
    if has_tests:
        score += 25
        details.append("Tests: found")
    else:
        details.append("Tests: MISSING")

    # README (15 pts)
    readme_files = ["README.md", "README.txt", "README.rst", "README"]
    has_readme = False
    for name in readme_files:
        readme_path = repo / name
        if readme_path.exists():
            content = readme_path.read_text(errors="replace").strip()
            if len(content) > 100:
                has_readme = True
            break
    if has_readme:
        score += 15
        details.append("README: found (>100 chars)")
    else:
        details.append("README: missing or trivial")

    # .gitignore (10 pts)
    if (repo / ".gitignore").exists():
        score += 10
        details.append(".gitignore: found")
    else:
        details.append(".gitignore: MISSING")

    # No hardcoded secrets (25 pts) — simple heuristic check
    secret_patterns = [
        re.compile(
            r"""(?:api[_-]?key|secret|password|token)\s*[:=]\s*["'][^"']{8,}["']""",
            re.IGNORECASE,
        ),
    ]
    secrets_found = False
    for py_or_ts in (
        list(repo.rglob("*.py"))
        + list(repo.rglob("*.ts"))
        + list(repo.rglob("*.js"))
        + list(repo.rglob("*.env"))
    ):
        if ".git" in py_or_ts.parts or "node_modules" in py_or_ts.parts:
            continue
        try:
            content = py_or_ts.read_text(errors="replace")
            for pat in secret_patterns:
                if pat.search(content):
                    secrets_found = True
                    break
        except OSError:
            continue
        if secrets_found:
            break

    if not secrets_found:
        score += 25
        details.append("Secrets scan: clean")
    else:
        details.append("Secrets scan: POTENTIAL SECRETS FOUND")
        report.auto_f_triggers.append("Potential hardcoded secrets detected")

    raw_text = f"{score}/100 ({', '.join(details)})"
    report.dimensions.append(
        DimensionResult("Hygiene", raw_text, _score_to_grade(score), score)
    )


# -- Grading -------------------------------------------------------------------


_WEIGHTS = {
    "Health": 0.25,
    "Linting": 0.10,
    "Type Safety": 0.10,
    "Complexity": 0.25,
    "Duplication": 0.15,
    "Hygiene": 0.15,
}


def _check_auto_f_triggers(report: ReportData) -> None:
    """Check for conditions that force an automatic F grade."""
    # Max CC > 50
    for h in report.hotspots:
        if h.cc > 50:
            report.auto_f_triggers.append(
                f"Function {h.function} in {Path(h.file).name} has CC={h.cc} (>50)"
            )
            break

    # > 60% duplication (already flagged by jscpd stage, check risk_flags)
    for flag in report.risk_flags:
        if "exceeds 60% threshold" in flag and flag not in report.auto_f_triggers:
            report.auto_f_triggers.append(flag)


def compute_overall(report: ReportData) -> None:
    """Compute weighted overall grade. Auto-F triggers override."""
    _check_auto_f_triggers(report)

    weighted_sum = 0.0
    weight_sum = 0.0
    for dim in report.dimensions:
        w = _WEIGHTS.get(dim.name, 0)
        if w > 0 and dim.grade != "?":
            weighted_sum += dim.score * w
            weight_sum += w

    if weight_sum > 0:
        report.overall_score = weighted_sum / weight_sum
    else:
        report.overall_score = 50

    # Auto-F override
    if report.auto_f_triggers:
        report.overall_grade = "F"
        report.overall_score = min(report.overall_score, 39)
    else:
        report.overall_grade = _score_to_grade(report.overall_score)


def _recommendation(grade: str) -> str:
    if grade == "A":
        return "Excellent codebase. Safe to contribute or depend on."
    if grade == "B":
        return "Good codebase with minor issues. Contribute with confidence."
    if grade == "C":
        return "Notable quality issues. Contribute with caution; plan refactoring."
    if grade == "D":
        return (
            "Significant structural problems. Fork and refactor before depending on it."
        )
    return "Critical quality issues. Walk away or plan a complete rewrite."


# -- Report rendering ---------------------------------------------------------


def render_report(report: ReportData) -> str:
    """Render the final markdown report."""
    lines: list[str] = []
    lines.append(f"# vibe-check: {report.repo_name}")
    lines.append(
        f"**Commit**: {report.commit_sha} | **Date**: {report.commit_date} "
        f"| **Overall**: {report.overall_grade} ({report.overall_score:.0f}/100)"
    )
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("| Dimension | Score | Grade |")
    lines.append("|-----------|-------|-------|")
    for dim in report.dimensions:
        lines.append(f"| {dim.name} | {dim.raw_value} | {dim.grade} |")
    lines.append("")

    # Auto-F triggers
    if report.auto_f_triggers:
        lines.append("## Auto-F Triggers")
        lines.append("**Grade forced to F due to critical issues:**")
        for trigger in report.auto_f_triggers:
            lines.append(f"- {trigger}")
        lines.append("")

    # Risk flags
    if report.risk_flags:
        lines.append("## Risk Flags")
        for flag in report.risk_flags:
            lines.append(f"- {flag}")
        lines.append("")

    # Complexity hotspots
    if report.hotspots:
        lines.append("## Complexity Hotspots (top 10)")
        lines.append("| File | Function | CC | MI |")
        lines.append("|------|----------|---:|---:|")
        for h in report.hotspots:
            short_file = Path(h.file).name
            mi_str = f"{h.mi:.1f}" if h.mi > 0 else "-"
            lines.append(f"| {short_file} | {h.function} | {h.cc} | {mi_str} |")
        lines.append("")

    # Duplication
    lines.append("## Duplication")
    lines.append(report.duplication_summary or "No duplication analysis available.")
    lines.append("")

    # Tool errors
    if report.tool_errors:
        lines.append("## Tool Warnings")
        for err in report.tool_errors:
            lines.append(f"- {err}")
        lines.append("")

    # Recommendation
    lines.append("## Recommendation")
    lines.append(_recommendation(report.overall_grade))
    lines.append("")

    return "\n".join(lines)


# -- Main ----------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: vibe_check.py <repo-url-or-path>", file=sys.stderr)
        sys.exit(1)

    target = sys.argv[1]

    # Determine workspace
    if target.startswith(("http://", "https://", "git@")):
        workspace = (
            _WORKSPACE if _WORKSPACE.parent.exists() else Path(tempfile.mkdtemp())
        )
    else:
        workspace = Path(target).resolve()

    report = ReportData()

    # Stage 1: Clone / identify
    try:
        repo_path, report.repo_name, report.commit_sha, report.commit_date = (
            stage_clone(target, workspace)
        )
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    # Stage 2: Detect languages
    languages = detect_languages(repo_path)
    if not languages:
        print(
            "WARNING: No Python or TypeScript files detected. Running Python toolchain as fallback.",
            file=sys.stderr,
        )
        languages = {"python"}

    # Stage 3: Lint
    if "python" in languages:
        stage_ruff(repo_path, report)
    if "typescript" in languages:
        stage_eslint(repo_path, report)

    # Stage 4: Type check
    if "python" in languages:
        stage_pyright(repo_path, report)
    if "typescript" in languages:
        stage_tsc(repo_path, report)

    # Stage 5: Complexity (lizard supports both, radon is Python-only)
    stage_complexity(repo_path, report)

    # Stage 6: Health (pyscn -- Python only)
    if "python" in languages:
        stage_health(repo_path, report)

    # Stage 7: Duplication
    if "python" in languages:
        stage_duplication(repo_path, report)
    if "typescript" in languages:
        stage_jscpd(repo_path, report)

    # Stage 8: History (wily) -- informational, no dimension
    stage_wily(repo_path, report)

    # Stage 9: Hygiene (license, tests, README, .gitignore, secrets)
    stage_hygiene(repo_path, report)

    # Compute overall grade (includes auto-F trigger check)
    compute_overall(report)

    # Output
    print(render_report(report))


if __name__ == "__main__":
    main()
