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
* Mutating dispatcher operations (label updates, `git worktree` creation, agent process launches) are executed by default (`--apply`). `--no-apply` skips those actions while saving a local result JSON and creating report directories/locks.
* The dispatch target (`--dispatch-target`) is automatically selected when unspecified based on runtime environment: `auto` for local/interactive runs (auto-detects local CLIs on PATH with `claude` preferred, `agy` second, `codex` third, and falls back with a warning to a dummy launch if none are found); `cloud-routine` (Claude Code Cloud Routine) when running in GitHub Actions (`GITHUB_ACTIONS=true`). Explicitly passing `local` triggers backward-compatible dummy launches (`true` no-op, for tests and dry-run purposes). Cloud targets support Claude Code Cloud Routine (`cloud-routine`, requiring `ORCHESTUNE_ROUTINE_TOKEN` env var, and `ORCHESTUNE_ROUTINE_ID` / `routine-id` in TOML) and Codex Cloud (`codex-cloud`, requiring `ORCHESTUNE_CODEX_CLOUD_ENV` or `codex-cloud-env` in TOML). `codex-cloud` pushes the task branch to `origin` before running `codex cloud exec`, and treats an open PR on the target branch as the completion signal. See the [Setup Guide](../../docs/en/setup.md#4-setting-up-codex-cloud) for details.
* The reviewer (`reviewer-bot` in configuration files) defaults to `auto` and is selected only after the dispatch target resolves: Claude targets request Codex review; Codex and `agy` targets request Claude review. Explicit `claude` or `codex` values in `orchestune.toml` override the mapping. Generic `local` cannot infer a reviewer and warns; custom `local-cmd` templates receive the resolved value only through an explicit `{reviewer_bot}` placeholder.
* Routine dispatch CLI options are strictly: `-p`/`--parent-issue`, `--apply`/`--no-apply`, `--dispatch-target`, `--max-concurrent`, `--profile`, and `--allow-unsafe-agent-execution` (for local CLI targets). All non-routine configuration is set via `orchestune.toml`.

## Workflow: Scheduled Dispatch Execution

1. Run the dispatcher to schedule and assign tasks to agents. Pass the required parent Issue number (`parent_issue_number` from the approved decomposition plan, or the parent Issue being resumed) via `-p` / `--parent-issue` (or execute from a `parent/issue-<number>` branch to infer it). This ensures child task branches diverge from the parent branch (`parent/issue-{number}`), enabling the Integrator to automatically merge completed child branches into the parent branch and close issues without waiting for human intervention (only the final merge from `parent/issue-{number}` to `main` requires human review).

   ```bash
   # Dry-run (preview changes without applying)
   orchestune-dispatch --no-apply -p <parent_issue_number>

   # Apply and launch parallel workspaces
   orchestune-dispatch -p <parent_issue_number>
   ```

2. stdout shows flushed progress by default, including on pipes; it is no longer JSON. Read the final JSON file instead of `dispatch | jq` or redirecting stdout as JSON. Human runs use the absolute `report saved` path. Automatic results use `report-dir` (default `.orchestune/reports/dispatch`, relative to the primary checkout), under `parent-<N>/<UTC timestamp>-<UUID>/result.json`. Do not search for the newest file or reuse a previous result.
   For machine processing, create a fresh session directory with the repository session-directory script and specify its unused file via `ORCHESTUNE_DISPATCH_REPORT_PATH` (relative paths use invocation cwd). Preserve the exit code before reading even a failed run's report:

   ```bash
   session_dir=$(./scripts/create-session-dir.sh dispatch-result <parent_issue_number>)
   result_path="$session_dir/dispatch-result.json"
   dispatch_code=0
   ORCHESTUNE_DISPATCH_REPORT_PATH="$result_path" orchestune-dispatch -p <parent_issue_number> || dispatch_code=$?
   if [ -f "$result_path" ]; then jq . "$result_path"; else echo "report not created" >&2; fi
   # dispatch_code remains available under set -e; file existence alone is not success.
   ```

   ```powershell
   $sessionDir = .\scripts\create-session-dir.ps1 dispatch-result <parent_issue_number>
   $resultPath = Join-Path $sessionDir 'dispatch-result.json'
   $env:ORCHESTUNE_DISPATCH_REPORT_PATH = $resultPath
   $savedPreference = $PSNativeCommandUseErrorActionPreference
   try {
       $PSNativeCommandUseErrorActionPreference = $false
       orchestune-dispatch -p <parent_issue_number>
       $dispatchCode = $LASTEXITCODE
   } finally {
       $PSNativeCommandUseErrorActionPreference = $savedPreference
       Remove-Item Env:ORCHESTUNE_DISPATCH_REPORT_PATH
   }
   if (Test-Path -LiteralPath $resultPath -PathType Leaf) { Get-Content -Raw $resultPath | ConvertFrom-Json }
   else { Write-Warning 'report not created' }
   ```

   Nonzero exit can still have a complete failure report; argument/configuration errors, reservation failure, or a cycle exception before a full report can leave no result. Each rerun needs a fresh path; existing files are rejected. The dedicated `<result-name>.report.lock` remains beside explicit results; exclusion covers cooperating dispatch runs, not unrelated external writers. Do not delete a lock while another execution may use it. Exit codes are 0 success, 1 fatal/save failure, 2 retryable failure (or argument/configuration error). `planned` means dry-run selection; `launched` means launch procedure committed, not task completion; `local` is a dummy target. Progress failure does not stop result saving.
3. If the state file `run_state.json` is lost (such as after GitHub Actions cache eviction), the dispatcher self-heals by reconstructing its execution state from `status:in-progress` GitHub Issues and open PR head branches, allowing dispatch to continue safely.
4. Read the current run's JSON and return dispatch outcomes (launched tasks, worktree paths, logs) to the [orchestune skill](../orchestune/SKILL.md) for final reporting to the user.
