# vibe-check

Code quality aggregator. Runs static analysis tools on Python and TypeScript repos and produces a graded report.

The inverse of "vibe code" (unauditable intent) -- this makes auditable intent visible.

## Usage

### Docker (recommended)

```bash
docker build -t vibe-check .
docker run --rm vibe-check https://github.com/org/repo
```

### Local

Requires: git, python 3.12+, and whichever tools you have installed. Missing tools are skipped gracefully.

Python tools: ruff, pyright, lizard, radon, pyscn, deepcsim-cli, wily.
TypeScript tools: eslint, tsc (typescript), jscpd.

```bash
python vibe_check.py https://github.com/org/repo
python vibe_check.py /path/to/local/repo
```

## What it runs

| Stage | Tool (Python) | Tool (TypeScript) | What it measures |
|-------|---------------|-------------------|-----------------|
| Lint | ruff | eslint | Style and correctness issues |
| Types | pyright | tsc | Type safety errors and warnings |
| Complexity | lizard + radon | lizard | Cyclomatic complexity and maintainability index |
| Health | pyscn | - | Overall codebase health (clones, dead code, cohesion, coupling) |
| Duplication | deepcsim-cli | jscpd | Structural duplication |
| History | wily | - | Complexity trends over git history |

Language is auto-detected from project markers (pyproject.toml, tsconfig.json, file extensions). Mixed-language repos run both toolchains.

## Grading

Each dimension gets a letter grade (A-F). Overall grade is a weighted average:

| Dimension | Weight |
|-----------|--------|
| Health | 30% |
| Complexity | 25% |
| Linting | 15% |
| Type Safety | 15% |
| Duplication | 15% |

## Output

Markdown report to stdout with: summary table, risk flags, complexity hotspots, duplication findings, and a recommendation.
