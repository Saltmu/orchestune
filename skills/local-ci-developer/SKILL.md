---
name: "local-ci-developer"
description: "Router skill orchestrating design/planning, issue filing, TDD, local CI, PR creation, automated review, and outcome reporting."
---

# Local CI & TDD Developer Skill

This skill routes the standard development workflow. [stages.toml](../../orchestune/workflow/stages.toml) owns stage metadata: entry conditions, actions, exit conditions and on-failure recovery, with mode/change/backend branches. Its [schema contract](../../docs/plans/local-ci-workflow-enforcement.md#stage-definitions-file) defines the fields; references below own action procedures. Stage data is guidance, never approval or executable state.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

> [!IMPORTANT]
> **No Direct GitHub Label Operations**:
> Never add, remove, or modify GitHub Issue or PR labels directly (e.g., never run `gh issue edit --add-label` / `gh issue edit --remove-label`). Label lifecycles and transitions are managed exclusively by `orchestune claim` and the Orchestune engine (Dispatcher and Integrator). Report every task outcome through `orchestune complete`, which posts the canonical Outcome Record to the task Issue.

## Execution Modes
| Item | Interactive Mode | Non-Interactive Mode (Auto-Dispatch / Existing Issue) |
| :--- | :--- | :--- |
| **Plan Approval (Step 1)** | Present plan to user and wait for approval; right after the start request (plan approval, or the user's instruction to start an existing Issue) and before claim, require explicit `claude` / `codex` / `skip` selection unless the request names one, and record it in the plan | When invoked with an existing Issue or Auto-Dispatch, bypass user approval after writing `<session-dir>/implementation-plan.md` and proceed directly to implementation; resolve reviewer bot from prompt/dispatch (Claude targets → Codex; Codex and `agy` targets → Claude, or explicit `reviewer-bot` setting) |
| **Issue Creation (Step 2)** | Create via selected backend (`gh` CLI or GitHub MCP/Web UI) if needed | Use issue number provided in prompt (skip creation) |
| **Worktree (Step 2.5)** | Check the initial footprint, then run `orchestune claim <issue_number>` to prepare and enter task worktree (proceed directly if already inside task worktree) | Check the initial footprint, then run `orchestune claim <issue_number>` to prepare and enter task worktree (proceed directly if already inside task worktree) |
| **Review Execution (Step 11)** | Use the selection recorded at the start request without re-asking; if none is recorded, require explicit `claude` / `codex` / `skip` selection first; no inference/default or review, merge, or completion before selection | Use reviewer bot resolved in Step 1; if unresolved, explicitly use `--bot-name skip` and stop for human review at the integration gate |
| **Escalation** | Prompt user for decision | Run `orchestune complete --issue <N> --result blocked --reason <REASON>` and terminate safely |

## Fast-Path for Minor Changes (Typo / Docs)
For documentation updates or typo fixes that do not alter code logic, **Steps 3–8 (TDD) may be skipped**. However, to prevent secret leaks (gitleaks) and ensure quality, **Step 9 Local CI (`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`) must always be executed** before proceeding to Step 10 (PR creation).

## Session Scratch Directory

### Temporary human-input waits

When waiting for user approval, reviewer selection, or an authorized exception, retain the active claim and worktree. Do not publish `complete --result blocked`
for that temporary wait: completion freezes the Outcome for its claim generation.
Use terminal `blocked` only when escalating work that cannot continue.
After a human resolves a completed blocked task, use the explicit local restart
procedure in [worktree.md](references/worktree.md#restart-a-completed-blocked-local-task), preserving its old Outcome and starting a new claim generation.

Before an Issue/worktree exists, create a repository-local, Git-ignored planning
directory via `./scripts/create-session-dir.sh planning <task>` (Windows: `.\scripts\create-session-dir.ps1 planning <task>`).
This generates `.orchestune/tmp/planning-<task>-<UTC timestamp>-<random>/` without inline Python.
`<planning-session-dir>` means this pre-claim directory. After Step 2.5 enters the task
worktree, create `.orchestune/tmp/task-<issue>-<UTC timestamp>-<random>/` via
`./scripts/create-session-dir.sh task <issue>` (Windows: `.\scripts\create-session-dir.ps1 task <issue>`),
migrate the approved `implementation-plan.md` into it, and use that explicit
`<session-dir>` for all remaining plan updates, PR bodies, review replies, and
scratch artifacts. Never resolve the pre-claim relative path after changing checkouts.
Never use a fixed file in the repository root or the OS-global `/tmp`, and never
delete and recreate a tracked fixed-name plan.

## Preflight & GitHub Backend Selection (Step 0)
At session start, inspect and record the execution environment:
1. **Tooling Availability**: Check `uv --version`, `uv lock --check`, and `gitleaks version`.
2. **GitHub Backend Selection**: Check `gh auth status` and GitHub MCP capabilities. Select either `gh` CLI or GitHub MCP as the fixed backend for all downstream GitHub operations throughout the session (Step 2 Issue Creation, Step 10 PR Creation, Step 12 Outcome Declaration), and record the choice in `<planning-session-dir>/implementation-plan.md`; migrate it to `<session-dir>/implementation-plan.md` after Step 2.5. If `gh` CLI is unauthenticated or unavailable, use GitHub MCP (or Web UI) without stalling.

## Initial Footprint (Before Issue Creation or Claim)
In Step 1, record design, preflight, selected backend/reviewer and expected changed files in the implementation plan; in Step 2, declare files as Footprint YAML in the Issue body. For existing Issues, check/correct the declaration before claim. Follow [the detailed procedure and YAML example](references/worktree.md#initial-footprint-procedure), including the explicit repository-reservation exception for unknown scope. Step 2.6 refines this initial declaration after claim.

## Development Steps
During the #822 observation period, read [measurement.md](references/measurement.md)
before Step 2.6 and maintain its record through Steps 10–12, including zero-finding PRs.
Prefer `orchestune workflow next --issue <N> --session-dir <session-dir> --json` and `workflow check` when available; if `orchestune workflow --help` fails on an older CLI, follow these prose steps. When a PR exists, pass `--pr` and applicable evidence paths. Treat status and actions as guidance; the commands never execute actions.

| Step | Item | Contract / stage | Procedure (source of truth) |
| :--- | :--- | :--- | :--- |
| **0–2** | **Preparation / Issue** | `prepare`: mode, backend, plan, reviewer and footprint; if already satisfied before claim, use `not-needed` from this checkout without creating a worktree. | Preflight, Execution Modes, Initial Footprint above |
| **2.5** | **Worktree Preparation** | `claim`: matching claim, footprint and worktree-local session. | [worktree.md](references/worktree.md) |
| **2.6** | **Impact Scope Determination** | `scope`: preserved initial table and reservation reconciliation. | [impact-scope.md](references/impact-scope.md) |
| **3–9** | **TDD & Local CI** | `implement`: code or docs path, current CI. | [tdd.md](references/tdd.md) (entrypoints: [local-ci.sh](../../scripts/local-ci.sh) / [local-ci.ps1](../../scripts/local-ci.ps1)) |
| **10** | **Pull Request Creation** | `pr`: required template, base and tested head. | [pr.md](references/pr.md) |
| **11** | **Automated LLM PR Review** | `review`: per-finding procedure and verified `review_target_sha`; when any finding was judged, `review-reply.md` must reach a PR comment. | [review-loop.md](references/review-loop.md) |
| **12** | **Outcome Declaration** | `complete`: `--reviewer` and `--review-reply` required for done; any judged finding's reply must already be posted as a PR comment. | Completion commands below |

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
`complete` preserves the worktree. Success means the matching result label and durable
handoff/replay receipt are confirmed, not PR merge, independent review approval, or
physical collection. Resume the same request with `--completion-id <ID>` and the matching claim marker (or the explicit completion ID for an unclaimed Issue);
replay does not restore labels after requeue. Without a Dispatcher, rerun GC to
advance policies; cloud/unclaimed not-needed needs independent approval.
See [worktree.md](references/worktree.md) for locks, review recovery and legacy holds.
