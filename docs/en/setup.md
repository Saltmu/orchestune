# Setup Guide

This guide describes how to install Orchestune, register its skills with various AI assistants (Claude Code, Codex CLI, Antigravity), and configure the cloud execution environment (Claude Code Cloud Routine).

---

## 0. Prerequisites

Orchestune is designed around the assumption that an agent implements according to a standard development workflow, and that child tasks passing CI and the child review-evidence gate (enabled by default) are integrated automatically. If the target repository does not meet the following prerequisites, you will not get the traceability and quality guarantees Orchestune is built for. Confirm these before adopting Orchestune.

1. **(a) A file defining the agent's development discipline must exist**
   Provide a file such as `AGENTS.md` / `CLAUDE.md` that documents the development workflow you want agents to follow in this repository (TDD, Issue filing, PR conventions, etc.). The instruction the Orchestune dispatcher sends to an agent is a single line — "implement this following the standard development workflow" — and the actual definition of that workflow is the target repository's responsibility. If you are starting from scratch, `orchestune setup --with-workflow-skill` (see Section 2 below) can drop in a generic template.
2. **(b) A quality gate (CI) thorough enough to be trusted with automatic merging**
   Orchestune has no human review gate at the child-task level. Alongside CI, the child review-evidence gate acts as an automated gate in the default `required` mode, verifying each child's passing review evidence (`verdict=pass`, matching the SHA to be merged) before updating the parent branch (see [Usage §4.5](usage.md#45-child-review-evidence-gate) and [Architecture & Design](architecture.md) for details). However, explicitly setting `child-review-gate = "off"` skips evidence verification, and even with the gate enabled, review judgments come from an LLM. Mechanically checking correctness therefore depends on CI; smoke-test-level CI alone cannot sufficiently guarantee the quality of code integrated automatically.
3. **(c) `ci_command` must be set to your repository's own CI entrypoint**
   The CI command the Integrator runs on the integration branch defaults to `./scripts/local-ci.sh`, which is specific to Orchestune's own repository. If your repository's CI entrypoint differs (e.g. `make ci`, `npm run ci`), set `ci_command` explicitly via `orchestune dispatch --ci-command "..."` or the `[tool.orchestune]` section of `orchestune.toml` / `pyproject.toml`.

### Creating and Editing the Configuration File

You can create or update your project-specific `orchestune.toml` using either the interactive wizard or manual file copying. Keep the real file out of Git because it may contain local targets or credentials, and publish shared changes through the example instead (also add it to `.gitignore` when adopting Orchestune in another repository).

#### Option A: Interactive Configuration Wizard (Recommended)

```bash
# Create a new configuration file
orchestune config init

# Edit existing configuration interactively
orchestune config edit

# Specify target project directory explicitly
orchestune config init --project-dir /path/to/project
```

- **Inheritance & Precedence**:
  - If `orchestune.toml` does not exist, `config init` migrates settings, comments, and table structure from `[tool.orchestune]` in `pyproject.toml`.
  - Once saved, `orchestune.toml` takes full precedence over any settings in `pyproject.toml`.
- **Safe Persistence & Conflict Prevention**:
  - The wizard acquires a cooperative lock (`.orchestune/config-write.lock`) and validates against the original file snapshot right before writing. If modified concurrently by another process, saving is aborted with exit code 3 to avoid silent overwrites.
  - When updating an existing file, a backup is created at `orchestune.toml.bak.<UTC timestamp>.<uuid>` before atomically replacing the file.
- **Cancellation**:
  - Choosing "cancel" in the preview menu, pressing `Ctrl+C` (exit code 130), or sending EOF cleanly aborts without creating or modifying configuration or backup files.

#### Option B: Manual Copy

```bash
cp orchestune.toml.example orchestune.toml
```

```toml
# Example orchestune.toml (see also orchestune.toml.example in the repository root)
ci-command = "make ci"
```

---

## 1. Installation

Orchestune requires Python 3.12+, uv, and the GitHub CLI (`gh auth status` must be authenticated).

### Using Orchestune in a Separate Project
To run `orchestune-dag` / `orchestune-dispatch` via an agent inside a separate project (e.g., a project named `manuscriptune`), follow these setup steps:

#### Step A: Install the CLI

```bash
# Install globally using uv tool (recommended)
uv tool install "orchestune==<RELEASE_VERSION>"

# Or using pipx
pipx install "orchestune==<RELEASE_VERSION>"

# Or add as a development dependency of the target project (uv)
uv add --dev orchestune
```

This makes the core `orchestune` command, as well as `orchestune-dag` and `orchestune-dispatch`, executable directly from that project's directory.

#### Windows Environment Support
Orchestune natively supports Windows NT/10/11 environments:
- **File Locking**: Cross-platform lock (`file_lock`) uses `msvcrt` on Windows and `fcntl` on POSIX.
- **Development & Local CI**: When developing Orchestune itself inside a cloned repository, PowerShell scripts `.\scripts\setup-git-hooks.ps1` and `.\scripts\local-ci.ps1` are available for local CI and Git hook setup (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).

---

## 2. Installing and Managing Skills for AI Assistants

The AI agent needs to know that the `orchestune`, `orchestune-provision`, and `orchestune-dispatch` skills exist. Orchestune provides a dedicated installer (`orchestune skills`) to distribute and manage skills for supported coding agents:
- **Codex CLI**: `.agents/skills/` (project) or `~/.agents/skills/` (user)
- **Antigravity IDE**: `.agents/skills/` (project) or `~/.gemini/config/skills/` (user)
- **Antigravity CLI**: `.agents/skills/` (project) or `~/.gemini/antigravity-cli/skills/` (user)
- **Claude Code**: `.claude/skills/` (project) or `~/.claude/skills/` (user)

> [!NOTE]
> Codex, Antigravity IDE, and Antigravity CLI share the canonical `.agents/skills/` directory at project scope. Orchestune automatically deduplicates targets to prevent redundant copies.

### Installing Skills (`orchestune skills install`)

```bash
# Preview changes without modifying disk
orchestune skills install --target all --scope project --dry-run

# Install to project directory (recommended for team sharing via Git)
orchestune skills install --target all --scope project

# Install to user configuration globally
orchestune skills install --target all --scope user

# Install for a specific assistant only
orchestune skills install --target codex --scope project
```

Project-scoped skills (`.agents/skills/` and `.claude/skills/`) can be committed to Git so all team members have access to the same skills. Local transaction state and lock files are maintained under `.orchestune-installer/` and should remain gitignored.

#### `--with-workflow-skill`: Deploying a generic workflow skill project-locally

If you need an agent discipline workflow for a non-Python repository or want a starting point for project-specific rules:

```bash
orchestune skills install --target all --scope project --with-workflow-skill
```

This installs `workflow-template` into project-local skills. Note that `workflow-template` cannot be installed globally (`--scope user`) since workflow rules are inherently project-specific.

### Managing and Inspecting Skills

```bash
# Check status of installed skills across all targets
orchestune skills status --target all --scope project

# Update skills when upgrading Orchestune package version
orchestune skills update --target all --scope project

# Uninstall skills
orchestune skills uninstall --target all --scope project

# Run system and configuration diagnostics
orchestune skills doctor --target all --scope project --offline
```

> [!TIP]
> **Migrating legacy symlinks/copies**: If skills were previously linked or copied using the older `orchestune setup` command or manual symlinks, run `orchestune skills install --target all --scope user --migrate-legacy` to safely upgrade them into managed skill directories. The legacy `orchestune setup` command is deprecated and delegates directly to `orchestune skills install`.

### Creating Scratch Directories (`orchestune scratch create`)

When subagents or skills create temporary planning, decomposition, or review artifacts, use the `orchestune scratch` command:

```bash
# Creates .orchestune/tmp/<artifact>-<issue-or-task>-<timestamp>-<uuid>/ and prints the path
orchestune scratch create plan 1191
```

This command verifies that `.orchestune/tmp/` is present in `.gitignore`, preventing accidental commits of temporary agent drafts.

---

## 3. Setting Up a Claude Code Cloud Routine

> [!NOTE]
> When `--dispatch-target` is not explicitly specified, `cloud-routine` from this section is automatically selected in a GitHub Actions environment (`GITHUB_ACTIONS=true`). If you run the dispatcher on GitHub Actions, set up the environment variables (Actions Secrets) below beforehand.

> [!IMPORTANT]
> Before firing a routine, the dispatcher now pushes the task branch (including any stacked/parent base content) to `origin` and verifies it landed, so that the cloud session starts from the correct base instead of the repository's default branch. This means the git credential the dispatcher process runs with (e.g. the checkout token in your workflow) needs **push access** (`contents: write`) to the repository — the default `permissions: contents: read` used by many CI workflows (including this repository's own `ci.yml`) is not sufficient on its own. If the push fails due to insufficient permission, the affected task is left as `status:blocked` with the underlying git error attached as a comment on its issue.

`--dispatch-target cloud-routine` is the target for **Claude Code Cloud Routine**.

1. **Create a New Routine**:
   Open [claude.ai/code/routines](https://claude.ai/code/routines) and click "New routine". You can use a minimal prompt body (the dispatcher sends the actual task instructions as `text` on every run).
2. **Add Repository**:
   Under "Repositories", add the GitHub repository you want to dispatch tasks against (the routine clones it from the default branch on every run).
3. **Add API Trigger**:
   Under "Select a trigger" -> "Add another trigger", choose **API**, then save the routine.
4. **Get Credentials**:
   After saving, copy the `routine_id` from the URL (`https://api.anthropic.com/v1/claude_code/routines/<routine_id>/fire`) and click "Generate token" to issue an API token.
5. **Set Environment Variables**:
   Set the routine ID and token as environment variables. If running in a CI environment like GitHub Actions, register them in your Actions Secrets:
   ```bash
   export ORCHESTUNE_ROUTINE_ID="<routine_id>"
   export ORCHESTUNE_ROUTINE_TOKEN="<token>"
   ```

> [!NOTE]
> The dispatcher always generates branch names in the `claude/issue-<issue_number>-<subtask_id>` format, which matches the routine's default branch-push restriction (only `claude/`-prefixed branches are allowed). You do not need to lift the branch restriction.

---

## 4. Setting Up Codex Cloud

`--dispatch-target codex-cloud` submits subtasks to a configured Codex Cloud environment through the Codex CLI.

1. Connect the target repository and create an environment in [Codex Cloud](https://chatgpt.com/codex).
2. Authenticate the local `codex` CLI with the same ChatGPT account.
3. Provide the environment ID through an environment variable or CLI option.

   ```bash
   export ORCHESTUNE_CODEX_CLOUD_ENV="<environment_id>"
   orchestune dispatch --dispatch-target codex-cloud
   # or
   orchestune dispatch --dispatch-target codex-cloud --codex-cloud-env "<environment_id>"
   ```

Before submission, Orchestune pushes the task branch to `origin`, then runs `codex cloud exec --env <environment_id> --branch <branch>` non-interactively. After submission, it tracks the actual Cloud task ID and URL, combining Cloud task status checks (early detection of failed / cancelled tasks) with branch PR / outcome record status to determine completion. If the environment ID is missing, Orchestune warns and safely falls back to the no-op target.

---

## 5. Setting Up Local `claude` / `agy` / `codex` CLI Dispatch

> [!NOTE]
> When `--dispatch-target` is not explicitly specified, outside of GitHub Actions (local/interactive runs) the dispatcher automatically selects `auto`, which detects and dispatches to whichever of `claude`/`agy`/`codex` is installed on `PATH` (preferring `claude`, then `agy`, then `codex`). If none are installed, it warns and falls back to the no-op dummy. To pin a specific CLI instead, pass `claude-cli`/`agy-cli`/`codex-cli` from this section explicitly.

### Prerequisite: Installing the `claude` CLI (Claude Code)

The presets in this section assume the `claude` command (Claude Code CLI) is already installed and on your PATH. If it isn't installed yet, install it with one of the following methods (see the [official documentation](https://docs.claude.com/) for details):

```bash
# Install globally via npm
npm install -g @anthropic-ai/claude-code
```

After installing, confirm the CLI is recognized with `claude --version`.

To dispatch subtasks to a local `claude`, `agy` (Antigravity), or `codex` (Codex CLI) session without hand-writing a `--local-cmd` template, use the built-in presets:

```bash
orchestune dispatch --dispatch-target claude-cli
# or
orchestune dispatch --dispatch-target agy-cli
# or
orchestune dispatch --dispatch-target codex-cli
# to auto-detect whichever CLI is installed, omit --dispatch-target or pass auto
orchestune dispatch --dispatch-target auto
```

These run `claude -p "..." --permission-mode bypassPermissions` / `agy -p "..." --add-dir . --print-timeout 60m --dangerously-skip-permissions` / `codex exec "..." --dangerously-bypass-approvals-and-sandbox` (non-interactive print/exec mode) in each subtask's own worktree. All presets always pass a permission-bypass flag so an unattended run never blocks on an interactive prompt.

By default, the resolved execution target also determines a cross-vendor PR reviewer: `claude-cli` and `cloud-routine` request Codex, while `codex-cli`, `codex-cloud`, and `agy-cli` request Claude. This selection happens after `--dispatch-target auto` resolves to a concrete target. Use `reviewer-bot = "claude"` or `reviewer-bot = "codex"` in your configuration file (`orchestune.toml`) to override it. A custom `local-cmd` may use the `{reviewer_bot}` placeholder; arbitrary custom commands are otherwise left unchanged.

> [!IMPORTANT]
> **Trust Model and Security Risks**
> 
> These local CLI targets run with full permissions, bypassing interactive approvals and sandboxes. To prevent accidental unrestricted execution, you must explicitly opt in by passing the `--allow-unsafe-agent-execution` CLI flag (setting this in configuration files is prohibited for safety). If this option is not specified, Orchestune will fail to start (fail-closed).
> 
> Note that a dedicated `git worktree` is only a boundary for isolating source code changes; it is **not** an OS-level security boundary (sandbox). An agent process running with bypassed permissions can access anything the host user has access to, including your home directory, credentials, other repositories, and network resources. For untrusted codebases/issues, or when running in shared/production environments, we strongly recommend wrapping Orchestune in a secure container or VM isolation layer.

There is no separate permission-file setup step required; `orchestune bootstrap` only ensures the required GitHub labels exist.

---

## 6. Scheduled Runs on GitHub Actions and Cross-Runner Serialization

As documented in [Integration Pipeline (architecture/integration.md)](architecture/integration.md#3-concurrency-control--design-assumptions), local file locks do not protect against concurrent runs across multiple CI runners/machines. If you run `orchestune dispatch` on a schedule, you must therefore satisfy the following single-executor contract operationally.

### 6.1 Single-Executor Contract (Supported Scope)

The standard supported configuration is **one control executor at a time per target repository**. Regardless of which parent Issue is targeted, every dispatch entrypoint that handles the same repository must share one serialization scope. Per-parent concurrency control and cross-repository (account-wide) quota protection are out of scope.

| Resource / processing | Required operational condition | Guarantee of existing mechanisms |
| --- | --- | --- |
| Candidate fetch, dependency/footprint checks, launch reservations, label/body updates, launch, recovery, GC | Serialize control processing for the same repository | The run-state lock covers the same local resource only. It does not protect a different path, clone, or machine |
| Not-needed review, Integrator, Semantic Review launch/recovery, parent branch updates, child/parent Issue completion | Keep serialization from dispatch start through the end of post-processing | Each local lock / ref-conflict detection is a local defense only |
| run-state, intent journals, review state, worktrees, running PIDs | One operational owner manages them continuously and hands the state over on transfer | Stopping concurrent runs alone cannot restore lost local state or PIDs on another machine |

- "Single executor" is a contract about control processing. The development agents of the selected child tasks may run in parallel. Ordinary `claim`/`complete` are not prohibited either (they follow their own existing ownership and state-lock contracts).
- A periodically run `orchestune gc` (which applies reservation releases by default) must be placed in the same serialization scope as dispatch (the same group on Actions). One-off operations such as `orchestune recover --apply` must be performed only after confirming that the control executor has stopped.
- In configurations outside the contract (concurrent runs with independent state), launch reservations may be overwritten and the same task may be selected twice.
- The run-state lock covers only the cycle part of `execute_cycle`. It does not cover the subsequent not-needed review polling, Integrator (including Semantic Review), parent Issue completion, or reporting. Serialization of the whole CLI is guaranteed operationally, by a single owner or an Actions group. The local lock does not protect the whole CLI.

### 6.2 Ownership Unit and Transfer

- The ownership unit is where run-state resolves to. A launch from a linked worktree or subdirectory of the same clone resolves to the primary checkout's `run_state.json`/`run_state.lock` via `claim/workspace.py:resolve_claim_workspace`, so it counts as the same owner. dispatch has no `--run-state` argument; the location is determined by the `run_state_path` setting (relative paths are based on the primary checkout).
- A different clone, a setup that points `run_state_path` at a different absolute path, and a different machine are independent owners. Locks on shared filesystems are not guaranteed.
- For local operation, start the whole CLI (including post-processing) serially from a single resident owner/scheduler. Do not treat concurrent launches of multiple CLIs as a supported setup just because a cycle lock exists.
- Transfer procedure: (1) stop new ticks → (2) confirm that the old control process and its post-processing have stopped → (3) check running tasks, reservations, and worktrees and carry the state over → (4) start the new owner. If the stop cannot be confirmed, do not start the new owner automatically.
- Combining Actions with a local CLI / Cloud Routine / external cron, or updating the same target from a workflow in another repository such as a fork, cannot be protected by an Actions group (unprotected).

### 6.3 Requirements for GitHub Actions

The standard concurrency is as follows (mapping form; do not use the string shorthand).

```yaml
concurrency:
  group: orchestune-control-${{ github.repository }}
  cancel-in-progress: false
```

- Do not put a parent Issue number, `github.workflow`, branch/ref, run ID, or runner name in the group. `schedule` and `workflow_dispatch` use the same group. If several workflows control the same target, they use the same group. Per-job independent groups and per-parent groups are not the standard configuration. The string shorthand (`concurrency: <group>`) cannot state `cancel-in-progress` explicitly, so do not use it.
- At the top level of the workflow, keep everything from the diagnostics through dispatch and post-processing inside a single run. Do not background it, hand it off asynchronously to a child workflow, or replicate it with a matrix.
- A pending run can be replaced by a newer run (GitHub's default queue behavior; FIFO and execution of every tick are not guaranteed). A running run is not interrupted thanks to `cancel-in-progress: false`, but manual cancellation, timeouts, and runner loss cannot be prevented. After an abnormal termination, check the state with the existing recovery procedure.
- To make replacement of pending runs harmless, a single run processes all target parents sequentially in the same step (splitting runs per parent would let parent A's pending run be replaced by parent B's run, starving parent A). The target parents are only the one given by the optional `workflow_dispatch` input `parent_issue`, or otherwise (including `schedule`) all parents in the repository variable `ORCHESTUNE_PARENT_ISSUES` (whitespace-separated positive integers). A parent given only manually is not re-run if the run is replaced; register parents under continuous operation in the variable and remove them when they are done.
- State `timeout-minutes` explicitly on the control job. One run handles all parents and the Integrator CI also runs for each parent, so leave headroom for "number of parents × Integrator timeout". A timeout can stop in the middle of post-processing, so check with the recovery procedure when it is exceeded.
- Control execution on Actions must use only external targets (`cloud-routine`/`codex-cloud`). Do not use local-process targets (`local`/`claude-cli`/`agy-cli`/`codex-cli`/`auto`), because the process and worktree are lost when the job ends. An external target only warns and falls back to a local launch when credentials cannot be resolved, so pass them from secrets to the control step's `env:` (`cloud-routine`: `ORCHESTUNE_ROUTINE_ID`/`ORCHESTUNE_ROUTINE_TOKEN`; `codex-cloud`: `ORCHESTUNE_CODEX_CLOUD_ENV`. The ID and Codex environment may instead be set via `routine_id`/`codex_cloud_env` in the config, but the token only via env).
- Receive `inputs`/`vars` through the step's `env:` and do not expand them directly into the `run:` body with `${{ }}` (script injection protection).

**Local state lost on the runner**: do not persist the following through caches or artifacts (`actions/cache` keys are immutable and last-writer-wins writes are not possible, so continuity of ownership cannot be proven). If the impact is not acceptable, use a persistent self-hosted runner or a local single-owner setup.

| Local state (default path) | Impact when lost |
| --- | --- |
| `task_reclaim_counts` in `run_state.json` (`run_state_path`) | Reclaim counts reset to 0 on every run, so `--max-task-reclaims` stops working across runs (tasks already moved to `status:blocked-human-review` by exceeding the limit keep the label and are not re-queued) |
| `active_worktrees`/`completed_worktrees`/`launch_history` in the same file | Rebuilt every time from GitHub labels, PRs, branches, and the parent Issue body. The launch-slot reservation history is also stored in the parent Issue body |
| `pending_lock_release_notices` in the same file | Re-sending of unsent external lock-release notices is lost |
| `completion_journal`/`completion_reservations`/`completion_replay_receipts`/`recovery_receipts` in the same file | The journal for resuming an in-flight complete and the reservation/replay/recovery receipts that GC cross-checks are lost. The canonical Outcome Record remains in Issue comments, but idempotent resumption or re-issuance from the journal/receipts is not guaranteed, so after an abnormal termination check with `orchestune recover` and [State Recovery](architecture/state-recovery.md) (whether each can be restored has not been confirmed) |
| `run_state.status-intents.json` (next to `run_state_path`) | The intent journal of an in-flight status repair is lost, so PLANNED/APPLIED intents cannot be reconciled in the next cycle. GitHub is the source of truth for actual labels, so re-observation re-plans them, but the reconciliation record of the intermediate state has not been confirmed |
| `not_needed_review_state.json` (`not_needed_review_state_path`) | The pending list of requested not-needed reviews is lost, so closing by polling the `not-needed-review:passed`/`failed` labels no longer happens. Tracking of requested reviews is cut off, so check the affected Issues manually |
| `events.jsonl` (`events_log_path`), `logs/` (`log_dir`), `.orchestune/reports/dispatch` (`report_dir`) | Only the history of logs and reports is lost |
| `worktrees/` (`worktree_root`) and `worktrees/.holds/` | Local-target worktrees and hold records are lost (the reason for restricting to external targets) |

#### 6.3.1 Example Workflow

[`examples/dispatch-single-executor.yml`](../examples/dispatch-single-executor.yml) is a workflow example that satisfies the requirements above (schedule plus manual entry, the shared top-level group, a single synchronous job, sequential processing of parents in the same step as the parent supply, and a self-diagnosis gate).

- Recommended copy destination: `.github/workflows/orchestune-dispatch.yml`. Another file name directly under `.github/workflows/` also works, because the gate derives its own path from `GITHUB_WORKFLOW_REF`. It runs `orchestune doctor --execution-mode actions --workflow <own path>` once before dispatch, and a diagnosis error or an empty credential (reported by name only, never by value) stops the job before any dispatch starts.
- Replace before enabling: `ORCHESTUNE_VERSION`, the cron expression, `timeout-minutes`, the secret names, and the dispatch target (to use `codex-cloud`, change both the target and the credential env names).
- Register the target parents in the repository variable `ORCHESTUNE_PARENT_ISSUES` (whitespace-separated positive integers) and remove parents that are done. A parent given only through the manual `parent_issue` input is not re-run if its pending run is replaced. An invalid or duplicate value fails the run without starting any dispatch; an empty value ends successfully; a failure of one parent does not stop the remaining ones, and the run exits non-zero at the end.
- Runs outside Actions (local CLI, Cloud Routine, external cron) cannot be stopped by this setup. Do not combine them, or transfer ownership first (see 6.2).
- This repository does not enable the example itself.

### 6.4 Migrating Existing Installations

The legacy per-parent group (`orchestune-integrate-…-${{ inputs.parent_issue }}`) fails this contract. The group-name prefix also changes, so the old and new workflows never share a group during migration. Switch over in this order:

1. Disable the old workflow (e.g. `gh workflow disable`) and stop new ticks.
2. Confirm that the old workflow has no running or pending runs. Wait for running ones to finish (do not cancel).
3. Delete the old workflow file from `.github/workflows/`. Enabled/disabled state cannot be determined by the offline diagnostics, so if a file containing a direct dispatch with a non-standard group remains, the new workflow's self-diagnostics stop with an error on `dispatch.repository.other_entrypoints`.
4. Add the new workflow and enable it only after confirming that `orchestune doctor` reports no errors.

### 6.5 Diagnostics with `orchestune doctor`

```bash
orchestune doctor --execution-mode actions --workflow .github/workflows/orchestune-dispatch.yml
orchestune doctor --execution-mode actions --workflow .github/workflows/orchestune-dispatch.yml --json
orchestune doctor --execution-mode local
```

`--workflow` may be given multiple times. The diagnostics are always offline: they make no GitHub API calls, perform no authentication checks, and change no state.

- Exit codes: `0` = no errors in the static configuration, `1` = configuration errors found, `2` = invalid arguments. Warnings/not_checked alone give `0`.
- **A configuration diagnosis is not a guarantee of operational ownership.** Even with exit code 0, the absence of conflicts with other machines, other clones, or entrypoints outside Actions has not been verified.
- Statuses are `ok`/`warning`/`error`/`not_checked` (cannot be determined statically; operational confirmation is required).

| code | Meaning (summary of when it is error / warning / not_checked) |
| --- | --- |
| `dispatch.config.readable` | error if loading/validating `orchestune.toml`/`pyproject.toml` fails (target and credentials cannot be determined either) |
| `dispatch.workflow.readable` | error for a missing or unreadable specified file, invalid YAML, or duplicate keys |
| `dispatch.actions.group` | ok if the top-level group matches the standard expression `orchestune-control-${{ github.repository }}`. error for the string shorthand, a missing or empty group, splitting by parent/workflow/ref/run, or mismatch between workflows |
| `dispatch.actions.cancel` | Only an explicit `cancel-in-progress: false` is ok. Missing, true, a string, or a dynamic expression is error |
| `dispatch.actions.parallelism` | error for a matrix on the job running direct dispatch, job-level concurrency, multiple control jobs, or background launches |
| `dispatch.actions.entrypoint` | ok if a synchronous dispatch entrypoint can be identified; not_checked if it cannot be detected |
| `dispatch.actions.target` | External targets (`cloud-routine`/`codex-cloud`) are ok, local-process targets are error, not_checked if it cannot be determined statically |
| `dispatch.actions.credentials` | ok if the credentials required by the external target are passed via `env:` visible to the control step; missing is error |
| `dispatch.local.serialization` | Always not_checked in local mode (confirm single ownership and serial execution of the whole CLI operationally) |
| `dispatch.repository.other_entrypoints` | Direct dispatch in unspecified workflows. warning for the standard group; error for a missing or non-standard group (including leftover old workflows). In local mode, warning if any workflow contains a direct dispatch |
| `dispatch.repository.other_control_entrypoints` | Detects `orchestune gc`/`orchestune recover --apply` in apply mode. ok for the standard group, warning otherwise |
| `dispatch.external_ownership` | Other machines, other clones, and entrypoints outside Actions are always not_checked |
| `dispatch.state.continuity` | Continuity of persistent state and worktrees/PIDs is always not_checked (see the table in 6.3) |

The direct dispatch entrypoints detected are `orchestune dispatch`, `orchestune-dispatch`, and `python -m orchestune.dispatch.dispatcher` (entrypoints with `--no-apply` are not counted as control executors). Variable expansion of the command name, lines that cannot be split, the inside of `uses:` actions or reusable workflows, and called scripts cannot be detected and therefore yield `not_checked`. In that case, manually confirm the execution entrypoint and the protected section through post-processing.

Note that this repository does not currently run its own `orchestune dispatch` on a GitHub Actions schedule (dispatch here goes through the Cloud Routine or a local CLI), so the above is a configuration for adopting repositories. If you enable a real scheduled run here, add a workflow file under `.github/workflows/` that includes the concurrency settings above.
