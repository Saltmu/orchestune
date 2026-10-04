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

If you build a workflow that runs `orchestune dispatch` on a GitHub Actions cron schedule, we strongly recommend configuring a `concurrency` group. As documented in [Integration Pipeline (architecture/integration.md)](architecture/integration.md#3-concurrency-control--design-assumptions) (design assumption #377), the integrator's mutual exclusion is enforced only by a same-machine file lock (`file_lock` in `orchestune/infra/process_utils.py`), which provides no protection across multiple CI runners/machines. A `concurrency` group gets you repository-wide (i.e. all-runner) serialization per parent Issue with no code changes.

```yaml
concurrency:
  # Group by the required parent Issue.
  group: orchestune-integrate-${{ github.repository }}-${{ inputs.parent_issue }}
  # Required: setting this to true would let a run cancel an in-flight integrator,
  # leaving the temp branch and worktree behind (`dispatch_gc` picks up more orphans,
  # and depending on when the cancel lands, the parent branch could end up partially
  # advanced).
  cancel-in-progress: false
```

> [!NOTE]
> GitHub Actions' `concurrency` only keeps "one running + one queued" run; a third trigger cancels whichever run was queued. This is harmless by design: the dispatcher reconstructs its state from GitHub (Issue labels/PRs/branches) every cycle, so a run cancelled out of the queue is equivalent to the next cron tick. It does not mean "a cycle is lost and processing stalls" — the next trigger picks up processing from the same state.

Note that this repository does not currently run its own `orchestune dispatch` on a GitHub Actions schedule (dispatch here goes through the Cloud Routine or a local CLI), so the snippet above is an example for adopting repositories. If you enable a real scheduled run here, add a workflow file under `.github/workflows/` that includes the `concurrency` block above.
