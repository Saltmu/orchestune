---
name: "orchestune-dispatch"
description: "Internal follow-up skill invoked by orchestune to schedule and dispatch eligible tasks to local/cloud coding agents."
version: "1.0.0"
category: "Development"
input_schema:
  type: "object"
  properties: {}
output_schema:
  type: "object"
  properties: {}
---

# Orchestune Dispatch Skill

This skill accepts filed GitHub Issues (or Issues filed by `orchestune-provision`) and handles dispatch configuration, worktree management, agent process invocation, and execution monitoring via the `orchestune-dispatch` CLI.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

## Trigger Conditions

**This is not normally a skill users invoke directly.** The [orchestune skill](../orchestune/SKILL.md) loads it internally as a handoff once decomposition and Issue provisioning are complete.

As an exception, a human may load this skill directly if they only want to re-run or resume dispatch for existing subtask issues (e.g. manual resumption after a lost state file, verifying a cron rerun).

## Prerequisites

* The `orchestune` CLI tools (`orchestune-dispatch`, `orchestune-dag`) must be installed on the system.
* The GitHub CLI (`gh` command) must be installed and authenticated (`gh auth status`).
* Mutating dispatcher operations (label updates, `git worktree` creation, agent process launches) are executed by default (`--apply`). To perform a dry run without side effects, specify `--no-apply` explicitly.
* The dispatch target (`--dispatch-target`) is automatically selected when unspecified based on runtime environment: `auto` for local/interactive runs (auto-detects local CLIs on PATH with `claude` preferred, `agy` second, `codex` third, and falls back with a warning to a dummy launch if none are found); `cloud-routine` (Claude Code Cloud Routine) when running in GitHub Actions (`GITHUB_ACTIONS=true`). Explicitly passing `local` triggers backward-compatible dummy launches (`true` no-op, for tests and dry-run purposes). Cloud targets support Claude Code Cloud Routine (`cloud-routine`, requiring `ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN`) and Codex Cloud (`codex-cloud`, requiring `ORCHESTUNE_CODEX_CLOUD_ENV` or `--codex-cloud-env`). `codex-cloud` pushes the task branch to `origin` before running `codex cloud exec`, and treats an open PR on the target branch as the completion signal. See the [Setup Guide](../../docs/en/setup.md) for details.

## Workflow: Scheduled Dispatch Execution

1. Run the dispatcher to schedule and assign tasks to agents. Always pass the parent Issue number (`parent_issue_number` from `decomposition_plan.md`, or the parent Issue being resumed) to `--parent-issue`. This ensures child task branches diverge from the parent branch (`parent/issue-{number}`), enabling the Integrator to automatically merge completed child branches into the parent branch and close issues without waiting for human intervention (only the final merge from `parent/issue-{number}` to `main` requires human review). If this flag is omitted, the dispatcher operates in flat mode (direct integration into `main`, always waiting for manual merge).

   ```bash
   # Dry-run (preview changes without applying)
   orchestune-dispatch --no-apply --parent-issue <parent_issue_number>

   # Apply and launch parallel workspaces
   orchestune-dispatch --parent-issue <parent_issue_number>
   ```

2. If the state file `run_state.json` is lost (such as after GitHub Actions cache eviction), the dispatcher self-heals by reconstructing its execution state from `status:in-progress` GitHub Issues and open PR head branches, allowing dispatch to continue safely.
3. Return dispatch outcomes (launched tasks, worktree paths, logs) to the [orchestune skill](../orchestune/SKILL.md) for final reporting to the user.
