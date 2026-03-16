# vibe-check

Detect AI slop and vibe-coded garbage before it hits your codebase.

## Why

AI code generation tools produce code that looks clean on the surface but carries structural debt underneath — loose types, copy-pasted functions, high complexity hidden behind formatted code. `vibe-check` runs 10 static analysis tools and produces a single grade (A-F) so you can tell at a glance whether a repo or PR is solid or vibe-coded.

## When to use

- **Before depending on a repo**: Clone it, grade it. F means walk away.
- **Reviewing a PR**: Run on the PR branch, compare to base. Did quality improve or degrade?
- **CI gate**: Block merges that drop the grade below a threshold.
- **Auditing your own code**: If your tool gives you an F, fix it before shipping.

## Usage

### Docker (recommended — all 10 tools pre-installed)

```bash
docker build -t vibe-check .

# Grade a repo
docker run --rm vibe-check https://github.com/org/repo

# Grade a local repo (mount it)
docker run --rm -v /path/to/repo:/workspace vibe-check /workspace
```

### Reviewing a PR

```bash
# Grade the base branch
git checkout main
docker run --rm -v $(pwd):/workspace vibe-check /workspace > base-report.md

# Grade the PR branch
git checkout feature-branch
docker run --rm -v $(pwd):/workspace vibe-check /workspace > pr-report.md

# Compare — did the PR improve or degrade quality?
diff base-report.md pr-report.md
```

Automated PR mode (run both, diff scores, post as PR comment) is planned — see [issue #3](https://github.com/nexus-marbell/vibe-check/issues/3).

### Local (partial — only runs tools you have installed)

```bash
python vibe_check.py https://github.com/org/repo
python vibe_check.py /path/to/local/repo
```

Missing tools are skipped gracefully. Docker is the intended workflow — all tools are pre-installed in the image.

## What it runs

| Stage | Python tool | TypeScript tool | What it measures |
|-------|-------------|-----------------|------------------|
| Lint | ruff | eslint | Style and correctness issues |
| Types | pyright | tsc --noEmit | Type safety errors |
| Complexity | lizard + radon | lizard | Cyclomatic complexity, maintainability index |
| Health | pyscn | — | Codebase health (dead code, cohesion, coupling) |
| Duplication | deepcsim | jscpd | Structural code duplication |
| Hygiene | built-in | built-in | License, tests, README, .gitignore, secrets |
| History | wily | — | Complexity trends over git history |

Language is auto-detected from project markers (`pyproject.toml`, `tsconfig.json`, file extensions). Mixed-language repos run both toolchains.

## Grading

Each dimension gets a letter grade (A-F). Overall grade is a weighted average:

| Dimension | Weight |
|-----------|--------|
| Health | 25% |
| Complexity | 25% |
| Hygiene | 15% |
| Duplication | 15% |
| Linting | 10% |
| Type Safety | 10% |

### Auto-F triggers

Any of these force the overall grade to F regardless of other scores:

- No license file (legal risk)
- Potential hardcoded secrets detected
- Any function with cyclomatic complexity > 50
- Code duplication > 60%

## Output

Markdown report to stdout: summary table, auto-F triggers, risk flags, complexity hotspots (file + function + CC), duplication findings, and a recommendation per grade.

## License

MIT License. See [LICENSE](LICENSE).
