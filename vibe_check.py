#!/usr/bin/env python3
"""vibe-check: Code quality aggregator that makes auditable intent visible.

Takes a git repo URL (or local path), runs static analysis tools,
and produces a graded markdown report. Supports full-repo mode,
PR mode (via GitHub URL), and manual ref comparison.

Usage:
    python vibe_check.py https://github.com/org/repo
    python vibe_check.py /path/to/local/repo
    python vibe_check.py --pr https://github.com/org/repo/pull/123
    python vibe_check.py --compare main...feature-branch https://github.com/org/repo
    python vibe_check.py --compare main...feature-branch /path/to/local/repo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

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
class CodebaseProfile:
    languages: set[str] = field(default_factory=set)
    markers: list[str] = field(default_factory=list)
    tool_plan: list[str] = field(default_factory=list)


@dataclass
class ReviewTarget:
    provider: str
    repo_url: str
    base_ref: str
    head_ref: str
    repo_slug: str


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


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with timeout. Returns CompletedProcess even on failure."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            cwd=cwd,
            env=env,
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


def _dim_unavailable(report: ReportData, name: str, raw_value: str, error: str) -> None:
    """Append a neutral unavailable dimension and preserve the tool failure."""
    report.tool_errors.append(error)
    report.dimensions.append(DimensionResult(name, raw_value, "?", 50))


def _score_issue_count(count: int) -> float:
    if count == 0:
        return 100
    if count <= 5:
        return 90
    if count <= 20:
        return 75
    if count <= 50:
        return 60
    if count <= 100:
        return 40
    return max(10, 40 - (count - 100) // 10)


def _score_error_warning_count(errors: int, warnings: int) -> float:
    total = errors + warnings
    if total == 0:
        return 100
    if errors == 0 and warnings <= 10:
        return 90
    if errors <= 5:
        return 75
    if errors <= 20:
        return 60
    if errors <= 50:
        return 40
    return max(10, 40 - (errors - 50) // 5)


# -- Language Detection --------------------------------------------------------

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".pyscn",
    ".wily",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
}


def _is_ignored_path(path: Path) -> bool:
    return any(part in _IGNORED_DIRS for part in path.parts)


def _source_files(repo: Path, suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for suffix in suffixes:
        files.extend(f for f in repo.rglob(f"*{suffix}") if not _is_ignored_path(f))
    return files


def _has_python_markers(repo: Path) -> bool:
    """Check for Python project markers or .py source files."""
    config_markers = ["pyproject.toml", "setup.py", "setup.cfg"]
    for marker in config_markers:
        if (repo / marker).exists():
            return True

    return len(_source_files(repo, (".py",))) > 0


def _has_typescript_markers(repo: Path) -> bool:
    """Check for TypeScript project markers or .ts/.tsx source files."""
    if (repo / "tsconfig.json").exists():
        return True

    if (repo / "package.json").exists():
        return True

    return len(_source_files(repo, (".ts", ".tsx"))) > 0


def _has_java_markers(repo: Path) -> bool:
    """Check for Java project markers or .java source files."""
    java_markers = [
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
    ]
    for marker in java_markers:
        if (repo / marker).exists():
            return True
    if (repo / "src" / "main" / "java").is_dir():
        return True
    return len(_source_files(repo, (".java",))) > 0


def build_codebase_profile(repo: Path) -> CodebaseProfile:
    """Detect languages and build the tool plan used by repo and PR/MR modes."""
    profile = CodebaseProfile()

    if _has_python_markers(repo):
        profile.languages.add("python")
        profile.markers.append("python")
        profile.tool_plan.extend(["ruff", "pyright", "pyscn"])

    if _has_typescript_markers(repo):
        profile.languages.add("typescript")
        profile.markers.append("typescript")
        profile.tool_plan.extend(["eslint", "tsc", "jscpd"])

    if _has_java_markers(repo):
        profile.languages.add("java")
        profile.markers.append("java")
        profile.tool_plan.extend(["java-ast", "java-build", "lizard"])

    profile.tool_plan.extend(["lizard", "hygiene", "hidden-code"])
    profile.tool_plan = list(dict.fromkeys(profile.tool_plan))
    return profile


def detect_languages(repo: Path) -> set[str]:
    """Detect programming languages in a repository.

    Returns a set containing detected languages such as "python",
    "typescript", and "java".
    """
    return build_codebase_profile(repo).languages


# -- Stage: Clone / Identify ---------------------------------------------------


def _inject_token(url: str) -> str:
    """Inject GH_TOKEN/GITHUB_TOKEN into HTTPS GitHub URLs for private repo access."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return url
    if url.startswith("https://github.com/"):
        return url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/"
        )
    return url


def _new_clone_workspace() -> Path:
    """Return an empty, writable directory for cloning a remote repository."""
    if (
        _WORKSPACE.exists()
        and os.access(_WORKSPACE, os.W_OK)
        and not any(_WORKSPACE.iterdir())
    ):
        return _WORKSPACE
    return Path(tempfile.mkdtemp(prefix="vibe-check-"))


def _git_auth_env(url: str) -> dict[str, str]:
    """Build non-interactive git auth env for provider-backed HTTPS clones."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return env
    if parsed.netloc == "github.com":
        return env
    if shutil.which("glab") is None:
        return env

    script_path = Path(tempfile.mkdtemp(prefix="vibe-check-auth-")) / "askpass.sh"
    env["VIBE_CHECK_GIT_HOST"] = parsed.netloc
    script_path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' oauth2 ;;\n"
        "  *Password*) printf 'protocol=https\\nhost=%s\\n\\n' \"$VIBE_CHECK_GIT_HOST\" | glab auth git-credential get | awk -F= '$1 == \"password\" {print $2; exit}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    env["GIT_ASKPASS"] = str(script_path)
    return env


def _trust_git_directory(repo: Path) -> None:
    """Allow git tools to inspect mounted repos owned by another host user."""
    _run(["git", "config", "--global", "--add", "safe.directory", str(repo)])


def stage_clone(target: str, workspace: Path) -> tuple[Path, str, str, str]:
    """Clone repo or resolve local path. Returns (path, name, sha, date)."""
    if target.startswith(("http://", "https://", "git@")):
        repo_name = target.rstrip("/").split("/")[-1].removesuffix(".git")
        clone_url = _inject_token(target)
        result = _run(
            ["git", "clone", "--depth=50", clone_url, str(workspace)],
            env=_git_auth_env(clone_url),
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
        repo_path = workspace
    else:
        local = Path(target).resolve()
        if not local.is_dir():
            raise RuntimeError(f"Local path not found: {target}")
        repo_path = local
        repo_name = local.name

    _trust_git_directory(repo_path)
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


# -- Stage: Hidden Code -------------------------------------------------------

_INVISIBLE_RANGES = (
    (0xFE00, 0xFE0F, "variation selector"),
    (0xE0100, 0xE01EF, "variation selector supplement"),
    (0xE0001, 0xE007F, "tag character"),
    (0x2061, 0x2064, "invisible operator"),
)

_INVISIBLE_CHARS = {
    0x00AD: "soft hyphen",
    0x180E: "mongolian vowel separator",
    0x200B: "zero-width space",
    0x200C: "zero-width non-joiner",
    0x200D: "zero-width joiner",
    0x2060: "word joiner",
}

_SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
)

_SINK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("python eval()", re.compile(r"\beval\s*\(")),
    ("python exec()", re.compile(r"\bexec\s*\(")),
    ("python compile()", re.compile(r"\bcompile\s*\(")),
    ("js Function()", re.compile(r"\b(?:new\s+)?Function\s*\(")),
    ("js eval()", re.compile(r"\beval\s*\(")),
    (
        "js string timer",
        re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*(['\"])", re.MULTILINE),
    ),
    (
        "java script engine eval",
        re.compile(r"\b(?:ScriptEngine|engine)\s*\.\s*eval\s*\("),
    ),
    (
        "shell execution",
        re.compile(r"\b(?:subprocess|Runtime\.getRuntime\(\)\.exec|ProcessBuilder)\b"),
    ),
)


def _invisible_category(char: str, index: int) -> str | None:
    codepoint = ord(char)
    if codepoint == 0xFEFF and index != 0:
        return "misplaced BOM"
    if codepoint in _INVISIBLE_CHARS:
        return _INVISIBLE_CHARS[codepoint]
    for start, end, label in _INVISIBLE_RANGES:
        if start <= codepoint <= end:
            return label
    return None


def _source_scan_files(repo: Path) -> list[Path]:
    return [f for f in _source_files(repo, _SOURCE_SUFFIXES) if f.is_file()]


def _scan_hidden_file(path: Path) -> tuple[dict[str, int], list[str]]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}, []

    invisible_counts: dict[str, int] = {}
    for index, char in enumerate(text):
        category = _invisible_category(char, index)
        if category:
            invisible_counts[category] = invisible_counts.get(category, 0) + 1

    sinks = [name for name, pattern in _SINK_PATTERNS if pattern.search(text)]
    return invisible_counts, sinks


def stage_hidden_code(repo: Path, report: ReportData) -> None:
    """Detect invisible Unicode plus dynamic execution sinks."""
    invisible_files: list[Path] = []
    sink_files: list[Path] = []
    critical_files: list[Path] = []
    invisible_total = 0
    sink_total = 0

    for path in _source_scan_files(repo):
        invisibles, sinks = _scan_hidden_file(path)
        if invisibles:
            invisible_files.append(path)
            invisible_total += sum(invisibles.values())
        if sinks:
            sink_files.append(path)
            sink_total += len(sinks)
        if invisibles and sinks:
            critical_files.append(path)

    penalty = len(invisible_files) * 5 + sink_total * 3
    score = max(0, 100 - penalty)
    if critical_files:
        score = min(score, 20)

    raw_value = (
        f"{len(invisible_files)} files with invisible Unicode, "
        f"{sink_total} execution sink{'s' if sink_total != 1 else ''}"
    )
    report.dimensions.append(
        DimensionResult("Hidden Code", raw_value, _score_to_grade(score), score)
    )

    if invisible_files:
        report.risk_flags.append(
            f"Invisible Unicode found in {len(invisible_files)} source file(s)"
        )
    if sink_files:
        report.risk_flags.append(
            f"Dynamic execution sinks found in {len(sink_files)} source file(s)"
        )
    if critical_files:
        names = ", ".join(sorted({p.name for p in critical_files[:5]}))
        report.auto_f_triggers.append(
            f"Invisible Unicode plus execution sink in same file ({names})"
        )


# -- Stage: Pyright ------------------------------------------------------------


def stage_pyright(repo: Path, report: ReportData) -> None:
    """Run pyright type checker, count errors."""
    result = _run(["pyright", ".", "--outputjson"], cwd=repo)
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


def _collect_lizard_cc(
    repo: Path, errors: list[str]
) -> tuple[list[int], list[HotspotEntry]]:
    """Run lizard and return (cc_values, hotspots)."""
    cc_values: list[int] = []
    hotspots: list[HotspotEntry] = []
    liz = _run(["lizard", str(repo), "--csv"])
    if liz.returncode == 127:
        errors.append("lizard: not installed")
        return cc_values, hotspots
    if not liz.stdout.strip():
        return cc_values, hotspots
    for line in liz.stdout.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            cc = int(parts[1].strip())
            func_name = parts[7].strip().strip('"')
            file_path = parts[6].strip().strip('"')
            cc_values.append(cc)
            if cc > 10:
                hotspots.append(
                    HotspotEntry(file=file_path, function=func_name, cc=cc, mi=0.0)
                )
        except (ValueError, IndexError):
            pass
    return cc_values, hotspots


def _collect_radon_cc(
    repo: Path, errors: list[str]
) -> tuple[list[int], list[HotspotEntry]]:
    """Run radon CC and return (cc_values, hotspots)."""
    cc_values: list[int] = []
    hotspots: list[HotspotEntry] = []
    result = _run(["radon", "cc", str(repo), "-j", "-a"])
    if result.returncode == 127:
        return cc_values, hotspots
    if not result.stdout.strip():
        return cc_values, hotspots
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        errors.append("radon cc: JSON parse error")
        return cc_values, hotspots

    for filepath, blocks in data.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            cc = block.get("complexity", 0)
            name = block.get("name", "?")
            cc_values.append(cc)
            if cc > 10:
                hotspots.append(
                    HotspotEntry(file=filepath, function=name, cc=cc, mi=0.0)
                )
    return cc_values, hotspots


def _collect_radon_mi(
    repo: Path, hotspots: list[HotspotEntry], errors: list[str]
) -> list[float]:
    """Run radon MI and return mi_values. Attaches MI to matching hotspots."""
    mi_values: list[float] = []
    result = _run(["radon", "mi", str(repo), "-j"])
    if result.returncode == 127:
        errors.append("radon: not installed")
        return mi_values
    if not result.stdout.strip():
        return mi_values
    try:
        mi_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        errors.append("radon mi: JSON parse error")
        return mi_values

    for filepath, mi_info in mi_data.items():
        if isinstance(mi_info, dict):
            mi_val = float(mi_info.get("mi", 0))
            mi_values.append(mi_val)
            for h in hotspots:
                if h.file == filepath:
                    h.mi = mi_val
        elif isinstance(mi_info, (int, float)):
            mi_values.append(float(mi_info))
    return mi_values


def _score_cc(avg_cc: float, max_cc: int) -> float:
    """Score cyclomatic complexity on 0-100 scale."""
    if avg_cc <= 3 and max_cc <= 10:
        return 95
    if avg_cc <= 5 and max_cc <= 15:
        return 80
    if avg_cc <= 8 and max_cc <= 20:
        return 65
    if avg_cc <= 12:
        return 45
    return 25


def _score_mi(avg_mi: float, has_data: bool) -> float:
    """Score maintainability index on 0-100 scale."""
    if not has_data:
        return 50  # no data = neutral
    if avg_mi >= 40:
        return 95
    if avg_mi >= 30:
        return 80
    if avg_mi >= 20:
        return 60
    if avg_mi >= 10:
        return 40
    return 20


def stage_complexity(repo: Path, report: ReportData) -> None:
    """Run lizard and radon for CC and MI metrics."""
    liz_cc, liz_hotspots = _collect_lizard_cc(repo, report.tool_errors)
    radon_cc, radon_hotspots = _collect_radon_cc(repo, report.tool_errors)

    cc_values = liz_cc + radon_cc
    hotspots = liz_hotspots + radon_hotspots
    max_cc = max(cc_values) if cc_values else 0

    mi_values = _collect_radon_mi(repo, hotspots, report.tool_errors)

    avg_cc = sum(cc_values) / len(cc_values) if cc_values else 0
    avg_mi = sum(mi_values) / len(mi_values) if mi_values else 0

    cc_score = _score_cc(avg_cc, max_cc)
    mi_score = _score_mi(avg_mi, bool(mi_values))

    combined = cc_score * 0.6 + mi_score * 0.4
    raw_text = f"avg CC {avg_cc:.1f}, max CC {max_cc}"
    if mi_values:
        raw_text += f", avg MI {avg_mi:.1f}"

    report.dimensions.append(
        DimensionResult("Complexity", raw_text, _score_to_grade(combined), combined)
    )

    mi_raw = f"avg MI {avg_mi:.1f}" if mi_values else "no data"
    report.dimensions.append(
        DimensionResult("Maintainability", mi_raw, _score_to_grade(mi_score), mi_score)
    )

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


def _parse_pyscn_text(text: str) -> float | None:
    """Extract health score from pyscn text output (e.g. 'Health Score: 84/100').

    Returns None if no match — distinguishes parser failure from a real 0/100.
    """
    match = re.search(r"Health\s+Score:\s*(\d+)/100", text)
    if match:
        return float(match.group(1))
    return None


def _find_pyscn_json(stdout: str, repo: Path) -> Path | None:
    """Locate the pyscn JSON report file from --json output.

    Primary: parse the "Unified JSON report generated: <path>" line pyscn prints.
    Fallback: glob for the newest analyze_*.json in <repo>/.pyscn/reports/.
    """
    match = re.search(r"JSON report generated:\s*(\S+\.json)", stdout)
    if match:
        path = Path(match.group(1))
        if path.exists():
            return path

    reports_dir = repo / ".pyscn" / "reports"
    if reports_dir.exists():
        candidates = sorted(
            reports_dir.glob("analyze_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def _parse_pyscn_json(json_path: Path) -> float | None:
    """Read summary.health_score from a pyscn JSON report. Returns None on failure."""
    try:
        with json_path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    score = data.get("summary", {}).get("health_score")
    if isinstance(score, (int, float)):
        return float(score)
    return None


def stage_health(repo: Path, report: ReportData) -> None:
    """Run pyscn health scanner.

    Parses the structured JSON report (primary) and falls back to text-regex on
    stdout (legacy). A parser failure is distinguished from a real 0/100 score:
    the dimension is marked "unparsed" with grade "?" and neutral score 50, and
    no Grade-F risk flag is added — see issue #17.
    """
    result = _run(["pyscn", "analyze", "--json", str(repo)])
    if result.returncode == 127:
        report.tool_errors.append("pyscn: not installed")
        report.dimensions.append(DimensionResult("Health", "skipped", "?", 50))
        return

    # Primary: read structured score from JSON report.
    score: float | None = None
    json_path = _find_pyscn_json(result.stdout, repo)
    if json_path is not None:
        score = _parse_pyscn_json(json_path)

    # Fallback: text-regex on combined stdout+stderr (legacy path).
    if score is None:
        score = _parse_pyscn_text(result.stdout + result.stderr)

    if score is None:
        # Tool ran but no score could be extracted. Surface the parser failure
        # rather than masquerading it as a real Grade-F finding.
        report.tool_errors.append("pyscn: could not parse health score")
        report.dimensions.append(DimensionResult("Health", "unparsed", "?", 50))
        return

    report.dimensions.append(
        DimensionResult("Health", f"{score:.0f}/100", _score_to_grade(score), score)
    )
    if score < 40:
        report.risk_flags.append(f"pyscn health score {score:.0f} (Grade F)")


# -- Stage: Duplication (pyscn JSON) -------------------------------------------


def _summarize_pyscn_clones(clones: list, limit: int = 10) -> list[str]:
    """Render up to `limit` lines describing clone fragments by file path.

    pyscn's clone JSON lists fragments (each participating in a clone group)
    rather than file pairs. Group fragments by file_path and emit one line
    per top file with a fragment count, for human-readable summaries.
    """
    counts: dict[str, int] = {}
    for clone in clones:
        if not isinstance(clone, dict):
            continue
        location = clone.get("location") or {}
        path = location.get("file_path", "")
        if path:
            counts[Path(path).name] = counts.get(Path(path).name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [f"- {name}: {n} clone fragment{'s' if n != 1 else ''}" for name, n in ranked]


def stage_duplication(repo: Path, report: ReportData) -> None:
    """Read duplication metrics from the pyscn JSON report.

    pyscn runs in stage_health (which precedes this stage for Python repos)
    and writes a JSON report to <repo>/.pyscn/reports/. We read the
    `summary.duplication_score`, `summary.clone_groups`, and
    `summary.code_duplication_percentage` fields directly from that report.

    Issue #16: previously ran deepcsim-cli with AST-shape similarity matching,
    which produced false-positive 100% matches between unrelated files at the
    default threshold (e.g., a test module flagged 100% similar to an unrelated
    production module purely on function-count/depth shape). The duplication
    dimension was therefore unusable as a quality signal. Replaced with pyscn's
    clone detection (APTED algorithm + content-aware), which has more
    conservative match semantics and reports actual code-duplication-percentage.
    """
    json_path = _find_pyscn_json("", repo)
    if json_path is None:
        # pyscn didn't write a JSON report (e.g., not installed; stage_health
        # would have flagged that already). Surface the missing signal cleanly
        # rather than masquerading as either success or a real F.
        report.tool_errors.append("pyscn: no JSON report (duplication stage)")
        report.dimensions.append(DimensionResult("Duplication", "unparsed", "?", 50))
        return

    try:
        with json_path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        report.tool_errors.append("pyscn: malformed JSON (duplication stage)")
        report.dimensions.append(DimensionResult("Duplication", "unparsed", "?", 50))
        return

    summary = data.get("summary", {}) or {}
    score_raw = summary.get("duplication_score")
    clone_groups = summary.get("clone_groups", 0) or 0
    duplication_pct = summary.get("code_duplication_percentage", 0) or 0

    if not isinstance(score_raw, (int, float)):
        report.tool_errors.append("pyscn: duplication_score missing from summary")
        report.dimensions.append(DimensionResult("Duplication", "unparsed", "?", 50))
        return

    score = float(score_raw)
    raw_value = f"{int(clone_groups)} clone group{'s' if clone_groups != 1 else ''} ({float(duplication_pct):.1f}% duplication)"
    report.dimensions.append(
        DimensionResult("Duplication", raw_value, _score_to_grade(score), score)
    )

    clones = (data.get("clone") or {}).get("clones") or []
    summary_lines = _summarize_pyscn_clones(clones)
    report.duplication_summary = (
        "\n".join(summary_lines) if summary_lines else "No significant duplication found."
    )

    if clone_groups > 5:
        report.risk_flags.append(f"{int(clone_groups)} clone groups detected")


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

    if result.returncode != 0 and not data:
        message = (result.stderr or result.stdout).strip().splitlines()
        detail = message[0] if message else f"exit {result.returncode}"
        _dim_unavailable(report, "Linting", "failed", f"eslint: {detail[:160]}")
        return

    count = 0
    fatal_count = 0
    for file_result in data:
        if isinstance(file_result, dict):
            count += file_result.get("errorCount", 0)
            fatal_count += file_result.get("fatalErrorCount", 0)

    score = _score_issue_count(count)

    report.dimensions.append(
        DimensionResult("Linting", f"{count} issues", _score_to_grade(score), score)
    )
    if result.returncode != 0 or fatal_count:
        report.tool_errors.append(
            f"eslint: exited {result.returncode} with {fatal_count} fatal errors"
        )
    if count > 50:
        report.risk_flags.append(f"High lint issue count ({count})")


# -- Stage: TypeScript dependency preparation ---------------------------------


def _ensure_node_dependencies(repo: Path, report: ReportData) -> bool:
    """Install TypeScript dependencies only when a reproducible lockfile exists."""
    if not (repo / "package.json").exists():
        return True
    if (repo / "node_modules").exists():
        return True

    if (repo / "package-lock.json").exists() or (repo / "npm-shrinkwrap.json").exists():
        result = _run(["npm", "ci", "--ignore-scripts"], cwd=repo)
        if result.returncode == 0:
            return True
        report.tool_errors.append(
            "npm ci: failed; TypeScript type check skipped "
            f"({(result.stderr or result.stdout).strip()[:160]})"
        )
        return False

    if (repo / "pnpm-lock.yaml").exists():
        report.tool_errors.append(
            "tsc: pnpm lockfile present but pnpm install is not managed by vibe-check"
        )
        return False
    if (repo / "yarn.lock").exists():
        report.tool_errors.append(
            "tsc: yarn lockfile present but yarn install is not managed by vibe-check"
        )
        return False

    report.tool_errors.append(
        "tsc: package.json present but node_modules and npm lockfile are missing"
    )
    return False


# -- Stage: tsc (TypeScript type checking) ------------------------------------


def stage_tsc(repo: Path, report: ReportData) -> None:
    """Run tsc --noEmit for type checking. Count errors."""
    if not _ensure_node_dependencies(repo, report):
        report.dimensions.append(
            DimensionResult("Type Safety", "deps unavailable", "?", 50)
        )
        return

    has_tsconfig = (repo / "tsconfig.json").exists()
    if has_tsconfig:
        cmd = ["tsc", "--noEmit", "--project", str(repo / "tsconfig.json")]
    else:
        # No tsconfig: check all TS files with permissive settings
        ts_files = [str(f) for f in _source_files(repo, (".ts", ".tsx"))]
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

    if result.returncode != 0 and errors == 0 and warnings == 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        detail = message[0] if message else f"exit {result.returncode}"
        _dim_unavailable(report, "Type Safety", "failed", f"tsc: {detail[:160]}")
        return

    score = _score_error_warning_count(errors, warnings)

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


# -- Stage: Java --------------------------------------------------------------

_JAVA_CLASS_PATTERN = re.compile(
    r"\b(?:class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_JAVA_METHOD_PATTERN = re.compile(
    r"(?:public|protected|private|static|final|synchronized|native|\s)+"
    r"[A-Za-z_][A-Za-z0-9_<>\[\], ?]*\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{"
)
_JAVA_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:System\.exit|Runtime\.getRuntime\(\)\.exec|ProcessBuilder|"
    r"Class\.forName|setAccessible\s*\(|ScriptEngine|Unsafe)\b"
)


def _java_files(repo: Path) -> list[Path]:
    return _source_files(repo, (".java",))


def stage_java_ast(repo: Path, report: ReportData) -> None:
    """Parse Java source structure and flag high-risk constructs."""
    java_files = _java_files(repo)
    if not java_files:
        report.dimensions.append(DimensionResult("Java AST", "no .java files", "?", 50))
        return

    class_count = 0
    method_count = 0
    risky_files: list[str] = []

    for path in java_files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        class_count += len(_JAVA_CLASS_PATTERN.findall(text))
        method_count += len(_JAVA_METHOD_PATTERN.findall(text))
        if _JAVA_HIGH_RISK_PATTERN.search(text):
            risky_files.append(str(path.relative_to(repo)))

    risk_count = len(risky_files)
    score = max(30, 100 - risk_count * 15)
    raw_value = (
        f"{len(java_files)} files, {class_count} types, "
        f"{method_count} methods, {risk_count} high-risk files"
    )
    report.dimensions.append(
        DimensionResult("Java AST", raw_value, _score_to_grade(score), score)
    )
    if risk_count:
        report.risk_flags.append(
            f"Java high-risk constructs in {risk_count} file(s): "
            + ", ".join(risky_files[:5])
        )


def _java_build_command(repo: Path) -> list[str] | None:
    if (repo / "mvnw").exists():
        return [str(repo / "mvnw"), "-q", "-DskipTests", "compile"]
    if (repo / "pom.xml").exists():
        return ["mvn", "-q", "-DskipTests", "compile"]
    if (repo / "gradlew").exists():
        return [str(repo / "gradlew"), "classes", "--quiet"]
    return None


def stage_java_semantics(repo: Path, report: ReportData) -> None:
    """Run Java build compilation as the semantic/LSP-equivalent signal."""
    cmd = _java_build_command(repo)
    if cmd is None:
        _dim_unavailable(
            report,
            "Type Safety",
            "skipped",
            "java: no Maven build file or Gradle wrapper found",
        )
        return

    result = _run(cmd, cwd=repo)
    if result.returncode == 127:
        _dim_unavailable(
            report,
            "Type Safety",
            "skipped",
            f"java build: command not found: {cmd[0]}",
        )
        return
    if result.returncode == 124:
        _dim_unavailable(report, "Type Safety", "timeout", "java build: timeout")
        return

    output = result.stdout + result.stderr
    error_lines = [
        line
        for line in output.splitlines()
        if re.search(r"\b(error|compilation failure)\b", line, re.IGNORECASE)
    ]
    if result.returncode != 0:
        count = max(1, len(error_lines))
        score = _score_error_warning_count(count, 0)
        report.dimensions.append(
            DimensionResult(
                "Type Safety",
                f"compile failed ({count} diagnostic lines)",
                _score_to_grade(score),
                score,
            )
        )
        report.risk_flags.append("Java compile failed")
        return

    report.dimensions.append(
        DimensionResult("Type Safety", "java compile passed", "A", 100)
    )


def stage_java_lsp(repo: Path, report: ReportData) -> None:
    """Probe for Java LSP availability; compile remains the semantic fallback."""
    if not _java_files(repo):
        return
    if shutil.which("jdtls") is None:
        report.tool_errors.append(
            "java lsp: jdtls not installed; using Maven/Gradle compile as semantic fallback"
        )
        report.dimensions.append(DimensionResult("Java LSP", "jdtls unavailable", "?", 50))
        return
    result = _run(["jdtls", "--version"], cwd=repo)
    if result.returncode in (0, 1):
        report.dimensions.append(DimensionResult("Java LSP", "jdtls available", "A", 100))
    else:
        _dim_unavailable(report, "Java LSP", "failed", "java lsp: jdtls probe failed")


# -- Stage: jscpd (TypeScript duplication) ------------------------------------


def _parse_jscpd_report(
    errors: list[str],
) -> tuple[float, int, list[str]]:
    """Parse the jscpd JSON report file.

    Returns (percentage, clone_count, summary_lines).
    """
    report_file = Path("/tmp/jscpd-report/jscpd-report.json")
    data: dict = {}
    if report_file.exists():
        try:
            data = json.loads(report_file.read_text())
        except json.JSONDecodeError:
            errors.append("jscpd: JSON parse error")

    statistics = data.get("statistics", {})
    total_stats = statistics.get("total", {})
    percentage = total_stats.get("percentage", 0)
    if not isinstance(percentage, (int, float)):
        percentage = 0

    clones = data.get("duplicates", [])
    clone_count = len(clones) if isinstance(clones, list) else 0

    summary_lines: list[str] = []
    if isinstance(clones, list):
        for dup in clones[:10]:
            if not isinstance(dup, dict):
                continue
            first = dup.get("firstFile", {})
            second = dup.get("secondFile", {})
            f1 = Path(first.get("name", "")).name if isinstance(first, dict) else "?"
            f2 = Path(second.get("name", "")).name if isinstance(second, dict) else "?"
            lines_count = dup.get("lines", 0)
            summary_lines.append(f"- {f1} <-> {f2} ({lines_count} lines)")

    return float(percentage), clone_count, summary_lines


def _score_duplication_pct(percentage: float) -> float:
    """Score duplication percentage on 0-100 scale."""
    if percentage == 0:
        return 100
    if percentage <= 3:
        return 90
    if percentage <= 5:
        return 80
    if percentage <= 10:
        return 70
    if percentage <= 20:
        return 55
    if percentage <= 40:
        return 35
    return max(10, 35 - int(percentage - 40))


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

    percentage, clone_count, summary_lines = _parse_jscpd_report(report.tool_errors)
    score = _score_duplication_pct(percentage)

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


def _check_has_tests(repo: Path) -> bool:
    """Check if the repo has a test directory or test files."""
    if any((repo / d).is_dir() for d in _TEST_DIRS):
        return True
    test_files = (
        list(repo.rglob("test_*.py"))
        + list(repo.rglob("*.test.ts"))
        + list(repo.rglob("*.spec.ts"))
        + list(repo.rglob("*Test.java"))
        + list(repo.rglob("*Tests.java"))
    )
    test_files = [f for f in test_files if not _is_ignored_path(f)]
    return len(test_files) > 0


def _check_has_readme(repo: Path) -> bool:
    """Check if the repo has a non-trivial README (>100 chars)."""
    for name in ("README.md", "README.txt", "README.rst", "README"):
        readme_path = repo / name
        if readme_path.exists():
            content = readme_path.read_text(errors="replace").strip()
            return len(content) > 100
    return False


_SECRET_PATTERN = re.compile(
    r"""(?:api[_-]?key|secret|password|token)\s*[:=]\s*["'][^"']{8,}["']""",
    re.IGNORECASE,
)


def _scan_for_secrets(repo: Path) -> bool:
    """Scan source files for potential hardcoded secrets."""
    globs = ("*.py", "*.ts", "*.js", "*.java", "*.kt", "*.env")
    for pattern in globs:
        for filepath in repo.rglob(pattern):
            if _is_ignored_path(filepath):
                continue
            try:
                content = filepath.read_text(errors="replace")
                if _SECRET_PATTERN.search(content):
                    return True
            except OSError:
                continue
    return False


def stage_hygiene(repo: Path, report: ReportData) -> None:
    """Check project hygiene: license, tests, README, .gitignore, secrets."""
    score = 0
    details: list[str] = []

    has_license = any((repo / name).exists() for name in _LICENSE_NAMES)
    if has_license:
        score += 25
        details.append("License: found")
    else:
        details.append("License: MISSING")
        report.auto_f_triggers.append("No license file (legal risk)")

    if _check_has_tests(repo):
        score += 25
        details.append("Tests: found")
    else:
        details.append("Tests: MISSING")

    if _check_has_readme(repo):
        score += 15
        details.append("README: found (>100 chars)")
    else:
        details.append("README: missing or trivial")

    if (repo / ".gitignore").exists():
        score += 10
        details.append(".gitignore: found")
    else:
        details.append(".gitignore: MISSING")

    if not _scan_for_secrets(repo):
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
    "Java AST": 0.10,
    "Hidden Code": 0.20,
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
        cap = 20 if any("Invisible Unicode" in t for t in report.auto_f_triggers) else 39
        report.overall_score = min(report.overall_score, cap)
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


# -- Analysis pipeline --------------------------------------------------------


def _run_analysis(repo_path: Path, repo_name: str, commit_sha: str, commit_date: str) -> ReportData:
    """Run the full analysis pipeline on a repo path.

    This is the single entry point for running all stages and computing
    the overall grade. Used by both full-repo mode and PR comparison mode.
    """
    report = ReportData(
        repo_name=repo_name,
        commit_sha=commit_sha,
        commit_date=commit_date,
    )

    profile = build_codebase_profile(repo_path)
    languages = profile.languages
    if not languages:
        print(
            "WARNING: No supported language markers detected. "
            "Running generic complexity, hygiene, and hidden-code checks.",
            file=sys.stderr,
        )

    if "python" in languages:
        stage_ruff(repo_path, report)
    if "typescript" in languages:
        stage_eslint(repo_path, report)

    if "python" in languages:
        stage_pyright(repo_path, report)
    if "typescript" in languages:
        stage_tsc(repo_path, report)
    if "java" in languages:
        stage_java_lsp(repo_path, report)
        stage_java_semantics(repo_path, report)

    stage_complexity(repo_path, report)
    if "java" in languages:
        stage_java_ast(repo_path, report)

    if "python" in languages:
        stage_health(repo_path, report)

    if "python" in languages:
        stage_duplication(repo_path, report)
    if "typescript" in languages:
        stage_jscpd(repo_path, report)

    stage_wily(repo_path, report)
    stage_hygiene(repo_path, report)
    stage_hidden_code(repo_path, report)
    compute_overall(report)

    return report


# -- Delta report -------------------------------------------------------------


def _direction_arrow(delta: float) -> str:
    """Return a direction arrow for a score delta."""
    if delta > 0:
        return "^"
    if delta < 0:
        return "v"
    return "="


def _diff_reports(base: ReportData, head: ReportData) -> str:
    """Compare two reports and produce a delta markdown report.

    Shows overall grade change, per-dimension deltas, new risk flags,
    new auto-F triggers, and new complexity hotspots.
    """
    lines: list[str] = []
    lines.append(f"# vibe-check: PR Delta -- {head.repo_name}")
    lines.append(
        f"**Base**: {base.commit_sha} | **Head**: {head.commit_sha}"
    )

    base_grade_str = f"{base.overall_grade} ({base.overall_score:.0f})"
    head_grade_str = f"{head.overall_grade} ({head.overall_score:.0f})"
    delta_overall = head.overall_score - base.overall_score
    arrow = _direction_arrow(delta_overall)
    lines.append(
        f"**Overall**: {base_grade_str} -> {head_grade_str} "
        f"{arrow} ({delta_overall:+.0f})"
    )
    lines.append("")

    # Per-dimension delta table
    lines.append("## Dimension Changes")
    lines.append("| Dimension | Base | Head | Delta |")
    lines.append("|-----------|------|------|-------|")

    base_dims = {d.name: d for d in base.dimensions}
    head_dims = {d.name: d for d in head.dimensions}
    all_dim_names = list(dict.fromkeys(
        [d.name for d in base.dimensions] + [d.name for d in head.dimensions]
    ))

    for name in all_dim_names:
        b = base_dims.get(name)
        h = head_dims.get(name)
        if b and h:
            delta = h.score - b.score
            arr = _direction_arrow(delta)
            lines.append(
                f"| {name} | {b.grade} ({b.score:.0f}) "
                f"| {h.grade} ({h.score:.0f}) | {delta:+.0f} {arr} |"
            )
        elif h:
            lines.append(f"| {name} | -- | {h.grade} ({h.score:.0f}) | NEW |")
        elif b:
            lines.append(f"| {name} | {b.grade} ({b.score:.0f}) | -- | REMOVED |")

    lines.append("")

    # New risk flags
    base_flags = set(base.risk_flags)
    new_flags = [f for f in head.risk_flags if f not in base_flags]
    if new_flags:
        lines.append("## New Risk Flags")
        for flag in new_flags:
            lines.append(f"- {flag} [NEW]")
        lines.append("")

    # Resolved risk flags
    head_flags = set(head.risk_flags)
    resolved_flags = [f for f in base.risk_flags if f not in head_flags]
    if resolved_flags:
        lines.append("## Resolved Risk Flags")
        for flag in resolved_flags:
            lines.append(f"- {flag} [RESOLVED]")
        lines.append("")

    # New auto-F triggers
    base_triggers = set(base.auto_f_triggers)
    new_triggers = [t for t in head.auto_f_triggers if t not in base_triggers]
    if new_triggers:
        lines.append("## New Auto-F Triggers")
        for trigger in new_triggers:
            lines.append(f"- {trigger} [NEW]")
        lines.append("")

    # New complexity hotspots
    base_hotspot_keys = {(h.file, h.function) for h in base.hotspots}
    new_hotspots = [
        h for h in head.hotspots if (h.file, h.function) not in base_hotspot_keys
    ]
    if new_hotspots:
        lines.append("## New Complexity Hotspots")
        for h in new_hotspots:
            short_file = Path(h.file).name
            lines.append(f"- {short_file}:{h.function} CC={h.cc} [NEW]")
        lines.append("")

    # Resolved hotspots
    head_hotspot_keys = {(h.file, h.function) for h in head.hotspots}
    resolved_hotspots = [
        h for h in base.hotspots if (h.file, h.function) not in head_hotspot_keys
    ]
    if resolved_hotspots:
        lines.append("## Resolved Complexity Hotspots")
        for h in resolved_hotspots:
            short_file = Path(h.file).name
            lines.append(f"- {short_file}:{h.function} CC={h.cc} [RESOLVED]")
        lines.append("")

    return "\n".join(lines)


# -- PR/MR URL parsing --------------------------------------------------------


def _parse_pr_url(url: str) -> tuple[str, str, int]:
    """Extract owner, repo, and PR number from a GitHub PR URL.

    Accepts URLs like:
        https://github.com/org/repo/pull/123
        https://github.com/org/repo/pull/123/files

    Raises ValueError if the URL does not match the expected format.
    """
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url
    )
    if not match:
        raise ValueError(
            f"Invalid PR URL: {url}\n"
            "Expected format: https://github.com/owner/repo/pull/123"
        )
    return match.group(1), match.group(2), int(match.group(3))


def _parse_gitlab_mr_url(url: str) -> tuple[str, str, int]:
    """Extract host, project path, and MR IID from a GitLab MR URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"Invalid GitLab MR URL: {url}\n"
            "Expected format: https://gitlab.example.com/group/project/-/merge_requests/123"
        )
    match = re.match(r"^/(.+)/-/merge_requests/(\d+)", parsed.path)
    if not match:
        raise ValueError(
            f"Invalid GitLab MR URL: {url}\n"
            "Expected format: https://gitlab.example.com/group/project/-/merge_requests/123"
        )
    project_path = match.group(1).strip("/")
    return parsed.netloc, project_path, int(match.group(2))


def _resolve_github_pr_refs(pr_url: str) -> ReviewTarget:
    """Resolve a GitHub PR URL to a provider-neutral review target."""
    owner, repo, number = _parse_pr_url(pr_url)
    repo_slug = f"{owner}/{repo}"

    result = _run(
        [
            "gh", "pr", "view", str(number),
            "--repo", repo_slug,
            "--json", "baseRefName,headRefName",
        ]
    )

    if result.returncode == 127:
        raise RuntimeError(
            "gh CLI not found. Install it from https://cli.github.com/ or "
            "use --compare BASE_REF...HEAD_REF instead."
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr view failed for {repo_slug}#{number}: "
            f"{result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse gh output: {exc}") from exc

    base_ref = data.get("baseRefName", "")
    head_ref = data.get("headRefName", "")
    if not base_ref or not head_ref:
        raise RuntimeError(
            f"Could not determine refs for {repo_slug}#{number}. "
            f"gh returned: {data}"
        )

    return ReviewTarget(
        provider="github",
        repo_url=f"https://github.com/{repo_slug}",
        base_ref=base_ref,
        head_ref=head_ref,
        repo_slug=repo_slug,
    )


def _resolve_gitlab_mr_refs(mr_url: str) -> ReviewTarget:
    """Resolve a GitLab MR URL to a provider-neutral review target."""
    host, project_path, iid = _parse_gitlab_mr_url(mr_url)
    encoded_project = quote(project_path, safe="")
    result = _run(
        [
            "glab",
            "api",
            "--hostname",
            host,
            f"projects/{encoded_project}/merge_requests/{iid}",
        ]
    )
    if result.returncode == 127:
        raise RuntimeError(
            "glab CLI not found. Install it from https://gitlab.com/gitlab-org/cli "
            "or use --compare BASE_REF...HEAD_REF instead."
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"glab api failed for {host}/{project_path}!{iid}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse glab output: {exc}") from exc

    base_ref = data.get("target_branch", "")
    head_ref = data.get("source_branch", "")
    if not base_ref or not head_ref:
        raise RuntimeError(
            f"Could not determine refs for {host}/{project_path}!{iid}. "
            f"glab returned: {data}"
        )

    return ReviewTarget(
        provider="gitlab",
        repo_url=f"https://{host}/{project_path}.git",
        base_ref=base_ref,
        head_ref=head_ref,
        repo_slug=f"{host}/{project_path}",
    )


def resolve_review_target(url: str) -> ReviewTarget:
    """Resolve a GitHub PR or GitLab MR URL to a common analysis target."""
    if re.match(r"https?://github\.com/[^/]+/[^/]+/pull/\d+", url):
        return _resolve_github_pr_refs(url)
    if "/-/merge_requests/" in url:
        return _resolve_gitlab_mr_refs(url)
    raise ValueError(
        f"Invalid review URL: {url}\n"
        "Expected a GitHub PR URL or GitLab MR URL."
    )


def _resolve_pr_refs(pr_url: str) -> tuple[str, str, str, str]:
    """Backward-compatible wrapper returning (repo_url, base_ref, head_ref, slug)."""
    target = _resolve_github_pr_refs(pr_url)
    return target.repo_url, target.base_ref, target.head_ref, target.repo_slug


# -- Comparison mode ----------------------------------------------------------


def _checkout_ref(
    repo_path: Path,
    ref: str,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Checkout a git ref and return (short_sha, date)."""
    fetch = _run(["git", "fetch", "origin", ref], cwd=repo_path, env=env)
    if fetch.returncode != 0:
        # Might be a local branch or tag, try checkout directly
        pass

    checkout = _run(["git", "checkout", ref], cwd=repo_path, env=env)
    if checkout.returncode != 0:
        # Try as remote tracking branch
        checkout = _run(["git", "checkout", f"origin/{ref}"], cwd=repo_path, env=env)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"Failed to checkout ref '{ref}': {checkout.stderr.strip()}"
            )

    log = _run(["git", "log", "-1", "--format=%H|%ci"], cwd=repo_path)
    if log.returncode == 0 and "|" in log.stdout.strip():
        sha, date = log.stdout.strip().split("|", 1)
        return sha[:12], date.split(" ")[0]
    return "unknown", "unknown"


def compare_refs(
    target: str, base_ref: str, head_ref: str, repo_slug: str = "",
) -> None:
    """Clone/resolve a repo, analyze base and head refs, print delta report.

    This is the core comparison logic used by both --pr and --compare modes.
    """
    is_remote = target.startswith(("http://", "https://", "git@"))
    if is_remote:
        workspace = _new_clone_workspace()
        clone_url = _inject_token(target)
        git_env = _git_auth_env(clone_url)
        result = _run(["git", "clone", clone_url, str(workspace)], env=git_env)
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
        repo_path = workspace
        repo_name = repo_slug or target.rstrip("/").split("/")[-1].removesuffix(".git")
    else:
        repo_path = Path(target).resolve()
        if not repo_path.is_dir():
            raise RuntimeError(f"Local path not found: {target}")
        repo_name = repo_slug or repo_path.name
        git_env = None

    _trust_git_directory(repo_path)

    # Analyze base ref
    print(f"Checking out base ref: {base_ref}", file=sys.stderr)
    base_sha, base_date = _checkout_ref(repo_path, base_ref, env=git_env)
    print(f"Analyzing base ({base_ref} @ {base_sha})...", file=sys.stderr)
    base_report = _run_analysis(repo_path, repo_name, base_sha, base_date)

    # Analyze head ref
    print(f"Checking out head ref: {head_ref}", file=sys.stderr)
    head_sha, head_date = _checkout_ref(repo_path, head_ref, env=git_env)
    print(f"Analyzing head ({head_ref} @ {head_sha})...", file=sys.stderr)
    head_report = _run_analysis(repo_path, repo_name, head_sha, head_date)

    # Produce delta report
    delta = _diff_reports(base_report, head_report)
    print(delta)


# -- CLI argument parsing -----------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for vibe-check."""
    parser = argparse.ArgumentParser(
        prog="vibe_check",
        description="Code quality aggregator. Analyzes repos and produces graded reports.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Repository URL or local path to analyze (full-repo mode).",
    )
    parser.add_argument(
        "--pr",
        metavar="PR_URL",
        help="GitHub PR or GitLab MR URL to analyze.",
    )
    parser.add_argument(
        "--compare",
        metavar="BASE...HEAD",
        help="Compare two refs (e.g., main...feature-branch). Requires target.",
    )
    return parser


# -- Main ----------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # PR mode
    if args.pr:
        try:
            review_target = resolve_review_target(args.pr)
        except (ValueError, RuntimeError) as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            compare_refs(
                review_target.repo_url,
                review_target.base_ref,
                review_target.head_ref,
                review_target.repo_slug,
            )
        except RuntimeError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Compare mode
    if args.compare:
        if not args.target:
            print(
                "FATAL: --compare requires a target (repo URL or local path).",
                file=sys.stderr,
            )
            sys.exit(1)
        parts = args.compare.split("...")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            print(
                f"FATAL: Invalid compare format: {args.compare}\n"
                "Expected: BASE_REF...HEAD_REF (e.g., main...feature-branch)",
                file=sys.stderr,
            )
            sys.exit(1)
        base_ref, head_ref = parts
        try:
            compare_refs(args.target, base_ref, head_ref)
        except RuntimeError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Full-repo mode (default)
    if not args.target:
        parser.print_help(sys.stderr)
        sys.exit(1)

    target = args.target

    if target.startswith(("http://", "https://", "git@")):
        workspace = _new_clone_workspace()
    else:
        workspace = Path(target).resolve()

    try:
        repo_path, repo_name, commit_sha, commit_date = stage_clone(target, workspace)
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    report = _run_analysis(repo_path, repo_name, commit_sha, commit_date)
    print(render_report(report))


if __name__ == "__main__":
    main()
