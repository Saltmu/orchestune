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
> Never add, remove, or modify GitHub Issue or PR labels (e.g., never run `gh issue edit --add-label` / `gh issue edit --remove-label`). Label lifecycles are managed exclusively by the Orchestune engine (Dispatcher and Integrator). Report every task outcome through `orchestune complete`, which posts the canonical Outcome Record to the task Issue.

This skill acts as a router orchestrating the standard development workflow: design planning, issue filing, TDD implementation, local CI verification, PR creation, automated LLM review, and final outcome reporting.

## Execution Modes
| Item | Interactive Mode | Non-Interactive Mode (Auto-Dispatch / Existing Issue) |
| :--- | :--- | :--- |
| **Plan Approval (Step 1)** | Present plan to user and wait for approval; right after the start request (plan approval, or the user's instruction to start an existing Issue) and before claim, require explicit `claude` / `codex` / `skip` selection unless the request names one, and record it in the plan | When invoked with an existing Issue or Auto-Dispatch, bypass user approval after writing `<session-dir>/implementation-plan.md` and proceed directly to implementation; resolve reviewer bot from prompt/dispatch or select cross-model distinct from author |
| **Issue Creation (Step 2)** | Create via selected backend (`gh` CLI or GitHub MCP/Web UI) if needed | Use issue number provided in prompt (skip creation) |
| **Worktree (Step 2.5)** | Check the initial footprint, then claim the task and use its worktree | Check the initial footprint before claim; use the existing task worktree if dispatcher-provisioned or already claimed |
| **Review Execution (Step 11)** | Use the selection recorded at the start request without re-asking; if none is recorded, require explicit `claude` / `codex` / `skip` selection first; no inference/default or review, merge, or completion before selection | Use reviewer bot resolved in Step 1; if unresolved, explicitly use `--bot-name skip` and stop for human review at the integration gate |
| **Escalation** | Prompt user for decision | Run `orchestune complete --issue <N> --result blocked --reason <REASON>` and terminate safely |

## Fast-Path for Minor Changes (Typo / Docs)
For documentation updates or typo fixes that do not alter code logic, **Steps 3–8 (TDD) may be skipped**. However, to prevent secret leaks and ensure quality, **Step 9 Local CI (`<CI_ENTRYPOINT>`) must always be executed** before proceeding to Step 10 (PR creation).

## Session Scratch Directory

Before writing any plan or CLI body file, generate the unique directory via
`orchestune scratch create <artifact> <issue-or-task>` as described in
[references/scratch.md](references/scratch.md). In the remainder of this skill,
`<session-dir>` means the unique directory defined there.

## Preflight & GitHub Backend Selection (Step 0)
At session start, inspect and record the execution environment:
1. **Tooling Availability**: Check `<PREFLIGHT_CHECK_COMMAND>` (e.g. package manager, lockfile consistency, secret scanner).
2. **GitHub Backend Selection**: Check `gh auth status` and GitHub MCP capabilities. Select either `gh` CLI or GitHub MCP as the fixed backend for all downstream GitHub operations throughout the session (Step 2 Issue Creation, Step 10 PR Creation, Step 12 Outcome Declaration), and record the choice in `<session-dir>/implementation-plan.md`. If `gh` CLI is unauthenticated or unavailable, use GitHub MCP (or Web UI) without stalling.

## Initial Footprint (Before Issue Creation or Claim)
In Step 1, list expected changed files; in Step 2, declare them as Footprint YAML in the Issue body. For existing Issues, check/correct the declaration before claim. Follow [the detailed procedure and YAML example](references/worktree.md#initial-footprint-procedure), including the explicit repository-reservation exception for unknown scope.

## Development Steps
| Step | Item | Summary / Command | Reference |
| :--- | :--- | :--- | :--- |
| **0** | **Preflight & Requirement Check** | Verify environment and tools via `<PREFLIGHT_CHECK_COMMAND>`, `gh auth status`, and GitHub MCP; fix backend. If requirements are already met before claim, run `orchestune complete --issue <N> --result not-needed` from the current checkout and exit without creating a worktree. | - |
| **1** | **Design & Implementation Plan** | Write `<session-dir>/implementation-plan.md` (preflight, backend, reviewer bot, design, initial footprint above). Ask user for plan approval, then obtain explicit reviewer selection right after the start request, before claim (bypass approval for existing Issue / Auto-Dispatch). | - |
| **2** | **GitHub Issue Creation** | For an existing Issue, verify/correct its footprint before claim. For a new Issue, include the Footprint YAML from the linked procedure and file via the selected backend (`gh issue create --title "..." --body "..."` or GitHub MCP/Web UI). | - |
| **2.5** | **Worktree Preparation** | Verify the initial footprint above, then run `orchestune claim <issue_number>` unless already inside the task worktree; perform all remaining work there. | [references/worktree.md](references/worktree.md) |
| **3–9** | **TDD & Local CI** | Reproducer test, baseline recording, test-driven implementation, local CI (`<CI_ENTRYPOINT>`). | [references/tdd.md](references/tdd.md) |
| **10** | **Pull Request Creation** | Fill `.github/pull_request_template.md` and submit via selected backend (`gh pr create` or GitHub MCP/Web UI). | [references/pr.md](references/pr.md) |
| **11** | **Automated LLM PR Review** | Atomic review trigger, wait, and feedback resolution loop (`wait_for_review.py` or fallback) using selected reviewer bot; Step 12 requires per-finding procedure Step 5 and verified `review_target_sha` (Exit 0 is acquisition only; Exit 11/30 or unknown SHA forbids done); when any finding was judged, post `review-reply.md` as a PR comment. | [references/review-loop.md](references/review-loop.md) |
| **12** | **Outcome Declaration** | From the claimed task worktree, run `orchestune complete --issue <N> --pr <PR> --result done --reviewer <claude|codex|skip> --review-reply <session-dir>/review-reply.md` after review (if any finding was judged, `review-reply.md` must already be posted as a PR comment); use the blocked command below if escalation is required. `complete` posts to Issue comments and hands off to GC. | - |

### Completion commands
Use `orchestune complete` for every outcome. It creates or reuses the canonical
Outcome Record in Issue comments; do not compose JSON or post the Outcome Record to PR comments (the `review-reply.md` PR comment of Step 11 is separate and still required).
Replace placeholders with the task Issue number, PR number, or concrete reason:

```bash
# Requirement already satisfied before claim: run from the current checkout.
orchestune complete --issue <N> --result not-needed

# Claude/Codex require the current-round judgment table; skip requires its PR marker (skipped, not pass).
orchestune complete --issue <N> --pr <PR> --result done --reviewer <claude|codex|skip> --review-reply <session-dir>/review-reply.md

# Claimed task: run from its worktree when work cannot continue.
orchestune complete --issue <N> --result blocked --reason <REASON>
```

For a claimed task whose requirement becomes unnecessary, run the `not-needed`
command from its worktree. `done` and `blocked` require an existing claim.
`complete` preserves the worktree and hands claimed completion to GC.
