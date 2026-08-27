---
name: "local-ci-developer"
description: "Router skill orchestrating design/planning, issue filing, TDD, local CI, PR creation, automated review, and outcome reporting."
---

# Local CI & TDD Developer Skill

This skill acts as a router orchestrating the standard development workflow: design planning, TDD implementation, local CI verification, PR creation, automated LLM review, and final outcome reporting.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

> [!IMPORTANT]
> **No Direct GitHub Label Operations**:
> Never add, remove, or modify GitHub Issue or PR labels (e.g., never run `gh issue edit --add-label` / `gh issue edit --remove-label`). Label lifecycles are managed exclusively by the Orchestune engine (Dispatcher and Integrator). All task outcomes (completion, escalation, or requirement already met) must be reported strictly through Outcome Records (`<!-- orchestune:outcome -->`).

## Execution Modes

| Item | Interactive Mode | Non-Interactive Mode (Auto-Dispatch) |
| :--- | :--- | :--- |
| **Plan Approval (Step 1)** | Present to user and wait for approval | Proceed immediately to implementation after writing `implementation_plan.md` |
| **Issue Creation (Step 2)** | Create via selected backend (`gh` CLI or GitHub MCP/Web UI) if needed | Use issue number provided in prompt (skip creation) |
| **Worktree (Step 2.5)** | Create and clean up a task worktree | Use the dispatcher-provisioned worktree; skip setup and cleanup |
| **Reviewer Selection (Step 11)** | Ask user to select reviewer (Claude/Codex) | Use the reviewer resolved by dispatch: Claude targets → Codex; Codex and `agy` targets → Claude. An explicit `reviewer-bot` setting overrides this mapping. |
| **Escalation** | Prompt user for decision | Post an outcome record (`blocked`) and terminate safely |

## Fast-Path for Minor Changes (Typo / Docs)
For documentation updates or typo fixes that do not alter code logic, **Steps 3–8 (TDD) may be skipped**. However, to prevent secret leaks (gitleaks) and ensure quality, **Step 9 Local CI (`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`) must always be executed** before proceeding to Step 10 (PR creation).

## Preflight & GitHub Backend Selection (Step 0)
At session start, inspect and record the execution environment:
1. **Tooling Availability**: Check `poetry --version`, `poetry check --lock`, and `gitleaks version`.
2. **GitHub Backend Selection**: Check `gh auth status` and GitHub MCP capabilities. Select either `gh` CLI or GitHub MCP as the fixed backend for all downstream GitHub operations throughout the session (Step 2 Issue Creation, Step 10 PR Creation, Step 12 Outcome Declaration), and record the choice in `implementation_plan.md`. If `gh` CLI is unauthenticated or unavailable, use GitHub MCP (or Web UI) without stalling.

## Development Steps

| Step | Item | Summary / Command | Reference |
| :--- | :--- | :--- | :--- |
| **0** | **Preflight & Requirement Check** | Verify Poetry, lockfile, gitleaks, `gh auth status`, and GitHub MCP; fix backend. If requirements are met on `main`, post outcome record (`result: not-needed`) and exit. | - |
| **1** | **Design & Implementation Plan** | Write `implementation_plan.md` recording preflight results, selected backend, and design. | - |
| **2** | **GitHub Issue Creation** | Skip if issue number was provided in prompt. When filing new: use selected backend (`gh issue create --title "..." --body "..."` or GitHub MCP/Web UI). | - |
| **2.5** | **Worktree Preparation** | For a requested change or existing Issue fix, create `worktree/<BRANCH_SLUG>` and perform all remaining work there. | [references/worktree.md](references/worktree.md) |
| **3–9** | **TDD & Local CI** | Reproducer test, baseline recording, test-driven implementation, local CI (`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`). | [references/tdd.md](references/tdd.md) |
| **10** | **Pull Request Creation** | Fill `.github/pull_request_template.md` and submit via selected backend (`gh pr create` or GitHub MCP/Web UI). | [references/pr.md](references/pr.md) |
| **11** | **Automated LLM PR Review** | Atomic review trigger, wait, and feedback resolution loop via `scripts/wait_for_review.py`. | [references/review-loop.md](references/review-loop.md) |
| **12** | **Outcome Declaration** | Post an outcome record (`result: done`) to PR/Issue comments via selected backend and finish work. | - |

### Outcome Record Format
Upon task completion, satisfaction, or escalation, post the appropriate machine-readable outcome marker and JSON payload in a comment. Field values for `issue` and `pr` must be unquoted numbers (e.g. `123`).

1. **Successful Completion (`result: "done"`)** — Post to **PR comments** (or Issue comments):
````markdown
<!-- orchestune:outcome -->
```json
{
  "result": "done",
  "issue": 123,
  "pr": 456
}
```
````

2. **Requirement Already Satisfied (`result: "not-needed"`)** — Post to **Issue comments** (no commit/PR created):
````markdown
<!-- orchestune:outcome -->
```json
{
  "result": "not-needed",
  "issue": 123
}
```
````

3. **Escalation / Blocked (`result: "blocked"`)** — Post to **Issue comments**. Set `base_sha` to current base commit SHA and increment `attempt` from prior outcome (1 on first failure; escalates at 3):
````markdown
<!-- orchestune:outcome -->
```json
{
  "result": "blocked",
  "issue": 123,
  "reason": "base-branch-red",
  "base_sha": "abc1234",
  "attempt": 1
}
```
````

