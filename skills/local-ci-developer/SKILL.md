---
name: "local-ci-developer"
description: "Router skill orchestrating design/planning, issue filing, TDD, local CI, PR creation, automated review, and outcome reporting."
---

# Local CI & TDD Developer Skill

This skill acts as a router orchestrating the standard development workflow: design planning, TDD implementation, local CI verification, PR creation, automated LLM review, and final outcome reporting.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

## Execution Modes

| Item | Interactive Mode | Non-Interactive Mode (Auto-Dispatch) |
| :--- | :--- | :--- |
| **Plan Approval (Step 1)** | Present to user and wait for approval | Proceed immediately to implementation after writing `implementation_plan.md` |
| **Issue Creation (Step 2)** | Create manually or via CLI if needed | Use issue number provided in prompt (skip creation) |
| **Reviewer Selection (Step 11)** | Ask user to select reviewer (Claude/Codex) | Automatically select a cross-model distinct from the author |
| **Escalation** | Prompt user for decision | Post an outcome record (`blocked`) and terminate safely |

## Fast-Path for Minor Changes (Typo / Docs)
For documentation updates or typo fixes that do not alter code logic, **Steps 3–8 (TDD) may be skipped**. However, to prevent secret leaks (gitleaks) and ensure quality, **Step 9 Local CI (`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`) must always be executed** before proceeding to Step 10 (PR creation).

## Development Steps

| Step | Item | Summary / Command | Reference |
| :--- | :--- | :--- | :--- |
| **0** | **Requirement Satisfaction Check** | If requirements are already met on `main`, do not create a PR; post an outcome record (`result: not-needed`) to the Issue and exit. | - |
| **1** | **Design & Implementation Plan** | Write `implementation_plan.md` and define the approach. | - |
| **2** | **GitHub Issue Creation** | Skip if issue number was provided in prompt. When filing new: `gh issue create --title "..." --body "..."`. | - |
| **3–9** | **TDD & Local CI** | Reproducer test, baseline recording, test-driven implementation, local CI (`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`). | [references/tdd.md](references/tdd.md) |
| **10** | **Pull Request Creation** | Fill `.github/pull_request_template.md` and submit via `gh pr create`. | [references/pr.md](references/pr.md) |
| **11** | **Automated LLM PR Review** | Atomic review trigger, wait, and feedback resolution loop via `scripts/wait_for_review.py`. | [references/review-loop.md](references/review-loop.md) |
| **12** | **Outcome Declaration** | Post an outcome record (`result: done`) to PR/Issue comments and finish work. | - |

### Outcome Record Format
Upon task completion, post the following machine-readable marker in a comment on the PR (or Issue):
```markdown
<!-- orchestune:outcome -->
```json
{
  "result": "done",
  "issue": 123,
  "pr": 456
}
```
```
