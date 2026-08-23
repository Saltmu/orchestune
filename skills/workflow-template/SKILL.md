---
name: "workflow-template"
description: "Generic template skill orchestrating design/planning, issue filing, TDD, local CI, PR creation, automated review, and outcome reporting. Edit command placeholders for the target project's language and tools before use."
---

# Workflow Template Skill

> [!IMPORTANT]
> This is a **generic template** placed by Orchestune's `orchestune setup --with-workflow-skill`. Replace placeholders enclosed in `<...>` (e.g. `<TEST_COMMAND>`, `<CI_ENTRYPOINT>`, `<FORMAT_LINT_COMMAND>`, `<TYPE_CHECK_COMMAND>`) with the actual commands for your project before use. You may also freely rename the folder and skill name (e.g. to `local-ci-developer`).

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

> [!IMPORTANT]
> **No Direct GitHub Label Operations**:
> Never add, remove, or modify GitHub Issue or PR labels (e.g., never run `gh issue edit --add-label` / `gh issue edit --remove-label`). Label lifecycles are managed exclusively by the Orchestune engine (Dispatcher and Integrator). All task outcomes (completion, escalation, or requirement already met) must be reported strictly through Outcome Records (`<!-- orchestune:outcome -->`).

This skill acts as a router orchestrating the standard development workflow: design planning, issue filing, TDD implementation, local CI verification, PR creation, automated LLM review, and final outcome reporting.

## Execution Modes

| Item | Interactive Mode | Non-Interactive Mode (Auto-Dispatch) |
| :--- | :--- | :--- |
| **Plan Approval (Step 1)** | Present to user and wait for approval | Proceed immediately to implementation after writing `implementation_plan.md` |
| **Issue Creation (Step 2)** | Create via selected backend (`gh` CLI or GitHub MCP/Web UI) if needed | Use issue number provided in prompt (skip creation) |
| **Worktree (Step 2.5)** | Create and clean up a task worktree | Use the dispatcher-provisioned worktree; skip setup and cleanup |
| **Reviewer Selection (Step 11)** | Ask user to select reviewer (Claude/Codex) | Automatically select a cross-model distinct from the author |
| **Escalation** | Prompt user for decision | Post an outcome record (`blocked`) and terminate safely |

## Fast-Path for Minor Changes (Typo / Docs)
For documentation updates or typo fixes that do not alter code logic, **Steps 3–8 (TDD) may be skipped**. However, to prevent secret leaks and ensure quality, **Step 9 Local CI (`<CI_ENTRYPOINT>`) must always be executed** before proceeding to Step 10 (PR creation).

## Preflight & GitHub Backend Selection (Step 0)
At session start, inspect and record the execution environment:
1. **Tooling Availability**: Check `<PREFLIGHT_CHECK_COMMAND>` (e.g. package manager, lockfile consistency, secret scanner).
2. **GitHub Backend Selection**: Check `gh auth status` and GitHub MCP capabilities. Select either `gh` CLI or GitHub MCP as the fixed backend for all downstream GitHub operations throughout the session (Step 2 Issue Creation, Step 10 PR Creation, Step 12 Outcome Declaration), and record the choice in `implementation_plan.md`. If `gh` CLI is unauthenticated or unavailable, use GitHub MCP (or Web UI) without stalling.

## Development Steps

| Step | Item | Summary / Command | Reference |
| :--- | :--- | :--- | :--- |
| **0** | **Preflight & Requirement Check** | Verify environment and tools via `<PREFLIGHT_CHECK_COMMAND>`, `gh auth status`, and GitHub MCP; fix backend. If requirements are met on `main`, post outcome record (`result: not-needed`) and exit. | - |
| **1** | **Design & Implementation Plan** | Write `implementation_plan.md` recording preflight results, selected backend, and design. | - |
| **2** | **GitHub Issue Creation** | Skip if issue number was provided in prompt. When filing new: use selected backend (`gh issue create --title "..." --body "..."` or GitHub MCP/Web UI). | - |
| **2.5** | **Worktree Preparation** | For a requested change or existing Issue fix, create `worktree/<BRANCH_SLUG>` and perform all remaining work there. | [references/worktree.md](references/worktree.md) |
| **3–9** | **TDD & Local CI** | Reproducer test, baseline recording, test-driven implementation, local CI (`<CI_ENTRYPOINT>`). | [references/tdd.md](references/tdd.md) |
| **10** | **Pull Request Creation** | Fill `.github/pull_request_template.md` and submit via selected backend (`gh pr create` or GitHub MCP/Web UI). | [references/pr.md](references/pr.md) |
| **11** | **Automated LLM PR Review** | Atomic review trigger, wait, and feedback resolution loop (`wait_for_review.py` or fallback). | [references/review-loop.md](references/review-loop.md) |
| **12** | **Outcome Declaration** | Post an outcome record (`result: done`) to PR/Issue comments via selected backend and finish work. | - |

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
