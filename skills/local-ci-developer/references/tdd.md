# TDD & Local CI Reference (Steps 3–9)

This document provides detailed procedures for Test-Driven Development (TDD) and local CI verification.

Perform every command in this document from the prepared worktree. Do not edit,
test, commit, or push from the primary checkout.

---

## 3. Reproducer Step
- **For bug fixes**: Before implementing any fix, create a minimal test under `tests/` that reproduces the reported defect and verify that it fails (Red).
- **For new features**: Skip this step and proceed to Step 4.

## 4. Baseline Recording (Baseline Record)
- **Prerequisites**: Confirm that the worktree environment is ready before recording the baseline:
  - Run `poetry check --lock` to verify lockfile consistency.
  - Run `poetry install` to ensure all dependencies and virtual environment scripts are available.
- Record the baseline status on unmodified code (select command based on your OS):
```bash
# Linux / macOS
poetry run python scripts/ci_baseline.py record

# Windows PowerShell
poetry run python scripts/ci_baseline.py record --ci-command "powershell -ExecutionPolicy Bypass -File .\\scripts\\local-ci.ps1"
```
- This record enables Step 9 to automatically distinguish between new regressions and pre-existing failures on the base branch.

## 5. Pre-Implementation Test Creation (Test-First)
- Write tests under `tests/` covering new features or revised specifications (happy path and major scenarios).
- Run `poetry run pytest` and verify that newly added tests fail as expected (Red).

## 6. Feature Implementation & Test Passing
- Implement the minimal code necessary to make the tests pass.
- Run `poetry run pytest` and verify that all tests pass (Green).

## 7. Failure Analyst (Root Cause Analysis on Repeated Failures)
- When the same test failure persists across 2 or more consecutive attempts, stop making uninformed changes and analyze:
  1. Direct cause of failure (stack trace, diff location)
  2. Hypothesis on why the expected fix did not resolve it
  3. Specific alternative approach for the next attempt
- `scripts/ci_baseline.py` automatically tracks consecutive failure counts. If not resolved after 3 analysis attempts, pause work and escalate.

## 8. Edge Case & Error Handling Coverage
- Strengthen test coverage by adding tests for boundary values, error conditions, and exception handling:
```bash
poetry run pytest --cov=orchestune --cov-branch --cov-report=term-missing
```

## 9. Comprehensive Local CI Verification & Error Resolution
- Execute baseline-aware CI verification (select command based on your OS):
```bash
# Linux / macOS
poetry run python scripts/ci_baseline.py check

# Windows PowerShell
poetry run python scripts/ci_baseline.py check --ci-command "powershell -ExecutionPolicy Bypass -File .\\scripts\\local-ci.ps1"
```
- Alternatively, run standard OS CI scripts (Linux/macOS: `./scripts/local-ci.sh`, Windows: `.\\scripts\\local-ci.ps1`).

### Error Resolution Procedures
1. **Ruff Format/Lint**:
   ```bash
   poetry run ruff format
   poetry run ruff check --fix
   ```
2. **Mypy Type Checking**:
   ```bash
   poetry run mypy orchestune tests
   ```
3. **Pytest Test Failures**:
   - `scripts/ci_baseline.py check` automatically categorizes new failures vs. baseline failures.
4. **Detect Bloat Warnings**:
   ```bash
   poetry run python scripts/detect_bloat.py --warn-only
   ```
   - If warnings for file size (code: 1000 lines, skill total: 500 lines) or function length (50 lines) are detected, **do not pause for user approval**; autonomously execute modular or prompt split refactoring to eliminate new or worsened bloat warnings before proceeding (if unresolved after 3 attempts, pause work and escalate). After refactoring, re-run verification procedures (steps 1–3) to ensure no regressions were introduced.
