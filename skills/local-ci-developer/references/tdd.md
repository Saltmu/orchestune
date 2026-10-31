# TDD & Local CI (Steps 3–9; implement stage)

Perform edits, tests, commits and pushes in the claimed task worktree.
Start: the current claim/worktree and preserved initial impact table are verified.
Actions: follow the code path below; docs/typos without logic changes may skip Steps 3–8, but never Step 9.
Finish: implementation and applicable tests pass, mandatory local CI passes before commit/push, and clean current-HEAD CI evidence is refreshed before PR/completion.
Failure: apply the bounded analysis and autonomous refactoring below; escalate after three unsuccessful analysis/refactoring attempts.

## 3. Reproducer
For a bug fix, add a minimal reproducer under `tests/` and verify failure (Red) before fixing. New features proceed to Step 4.

## 4. Baseline
Prerequisites: `uv lock --check` and `uv sync` succeed so dependencies and environment scripts exist.
Record unmodified-code CI to distinguish pre-existing failures from regressions:
- Linux/macOS: `uv run python scripts/ci_baseline.py record`.
- Windows: `uv run python scripts/ci_baseline.py record --ci-command "powershell -ExecutionPolicy Bypass -File .\\scripts\\local-ci.ps1"`.

## 5–6. Test first, then implement
Add happy-path and major-scenario tests under `tests/`; verify the new tests fail with `uv run pytest` (Red).
Implement the minimal change, then verify tests pass (Green).

## 7–8. Failure analysis and boundaries
After the same failure persists twice, stop uninformed edits: record its direct cause, why the prior hypothesis failed, and a concrete alternative. `ci_baseline.py` tracks consecutive failures; after three unsuccessful analysis attempts escalate.
Add boundary/error scenarios and inspect `uv run pytest --cov=orchestune --cov-branch --cov-report=term-missing`.

## 9. Mandatory local CI
Determine the execution OS. Before committing or pushing, run Linux/macOS `./scripts/local-ci.sh` or Windows `.\\scripts\\local-ci.ps1` and confirm zero errors.
Baseline-aware equivalent: `uv run python scripts/ci_baseline.py check`; on Windows append `--ci-command "powershell -ExecutionPolicy Bypass -File .\\scripts\\local-ci.ps1"`.
Resolve regressions and record baseline failures; baseline classification never waives required quality gates.
- Format/lint: `uv run ruff format`, `uv run ruff check --fix`.
- Types: `uv run mypy orchestune tests`.
- Tests: inspect baseline-aware failure classification and repair new failures.
- Bloat: `uv run python scripts/detect_bloat.py --warn-only`. On edits or CI warnings, autonomously split modules/prompts to remove new/worsened warnings (code file 1000 lines, skill Markdown total 500, function 50); re-run format, lint, types and tests. After three unsuccessful refactoring attempts escalate.

## Evidence recovery
CI followed by a fix, commit/amend or rebase: prior CI bindings are stale. Run the required checks before committing, then refresh local CI for the resulting clean HEAD; never reuse an older SHA's evidence for PR/completion.
