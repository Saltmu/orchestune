# TDD & Local CI Reference (Steps 3–9)

This document provides detailed procedures for Test-Driven Development (TDD) and local CI verification. Replace placeholders (`<...>`) with the appropriate commands for your project.

Perform every command in this document from the prepared worktree. Do not edit,
test, commit, or push from the primary checkout.

---

## 3. Reproducer Step
- **For bug fixes**: Before implementing any fix, create a minimal test or script reproducing the reported defect, and run `<TEST_COMMAND>` to verify that it fails (Red).
- **For new features**: Skip this step and proceed to Step 4.

## 4. Baseline Recording (Baseline Record)
- Record the baseline status on unmodified code.
- **When using Orchestune / dedicated baseline scripts**:
  ```bash
  poetry run python scripts/ci_baseline.py record
  ```
- **Fallback procedure (when no baseline script exists)**:
  Run `<TEST_COMMAND>` on unmodified code and note existing test failures (failures or flaky tests unrelated to this issue) in a temporary note. Use this record in Step 9 to distinguish new regressions.

## 5. Pre-Implementation Test Creation (Test-First)
- Write tests covering new features or revised specifications (happy path and major scenarios).
- Run `<TEST_COMMAND>` and verify that newly added tests fail as expected (Red).

## 6. Feature Implementation & Test Passing
- Implement the minimal code necessary to make the tests pass.
- Run `<TEST_COMMAND>` and verify that all tests pass (Green).

## 7. Failure Analyst (Root Cause Analysis on Repeated Failures)
- When the same test failure persists across 2 or more consecutive attempts, stop making uninformed changes and analyze:
  1. Direct cause of failure (stack trace, diff location)
  2. Hypothesis on why the expected fix did not resolve it
  3. Specific alternative approach for the next attempt
- If not resolved after 3 analysis attempts, pause work and escalate (outcome `blocked` in non-interactive mode, or ask the user in interactive mode).

## 8. Edge Case & Error Handling Coverage
- Strengthen test coverage by adding tests for boundary values, error conditions, and exception handling according to your project's coverage targets.

## 9. Comprehensive Local CI Verification & Error Resolution
- Execute the project's integrated local CI command:
  ```bash
  <CI_ENTRYPOINT>
  ```
- In script-supported environments, run `poetry run python scripts/ci_baseline.py check` to evaluate results against the recorded baseline.

### Error Resolution Procedures
1. **Format/Lint**: Run `<FORMAT_LINT_COMMAND>` and fix any unresolved errors.
2. **Type Checking**: Run `<TYPE_CHECK_COMMAND>` and resolve type mismatches.
3. **Test Failures**: Identify and fix `<TEST_COMMAND>` failures. Qualification is Baseline-aware (zero new failures introduced beyond baseline failures).
4. **Bloat Warnings**: If file size or complexity warnings are detected, pause work and present a modular refactoring plan.
