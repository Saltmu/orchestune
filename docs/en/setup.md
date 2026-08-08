# Setup Guide

This guide describes how to install Orchestune, register its skills with various AI assistants (Claude Code, Codex CLI, Antigravity), and configure the cloud execution environment (Claude Code Cloud Routine).

---

## 1. Installation

Orchestune requires Python 3.12+, Poetry, and the GitHub CLI (`gh auth status` must be authenticated).

### Using Orchestune in a Separate Project
To run `orchestune-dag` / `orchestune-dispatch` via an agent inside a separate project (e.g., a project named `manuscriptune`), follow these setup steps:

#### Step A: Install the CLI

```bash
# Install globally using pipx (recommended)
pipx install git+https://github.com/Saltmu/orchestune.git

# Or add as a development dependency of the target project (Poetry)
poetry add --group dev git+https://github.com/Saltmu/orchestune.git
```

This makes the core `orchestune` command, as well as `orchestune-dag` and `orchestune-dispatch`, executable directly from that project's directory.

#### Windows Environment Support
Orchestune natively supports Windows NT/10/11 environments:
- **File Locking**: Cross-platform lock (`file_lock`) uses `msvcrt` on Windows and `fcntl` on POSIX.
- **Development & Local CI**: When developing Orchestune itself inside a cloned repository, PowerShell scripts `.\scripts\setup-git-hooks.ps1` and `.\scripts\local-ci.ps1` are available for local CI and Git hook setup (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).

---

## 2. Registering Skills with AI Assistants

The AI agent needs to know that the `orchestune`, `orchestune-dispatch`, and `local-ci-developer` skills exist. Choose one of the following methods to register them:

### Method A: Automatic Setup (Recommended)
Run the setup command to automatically create symlinks in the global configuration directories of all supported AI assistants (Claude Code, Codex CLI, Antigravity). `local-ci-developer` is excluded from automatic linking:

```bash
orchestune setup
```

### Method B: Manual Setup (Per Project or Global)

* **`.agents/skills.json`** (For Antigravity):
  In the target project, add an entry to `.agents/skills.json` pointing to this repository's `skills/` directory:
  ```json
  {
    "entries": [
      { "path": "../path/to/cloned/orchestune/skills" }
    ]
  }
  ```

* **Project-local Skills** (For Claude Code and Codex CLI):
  Both agents natively auto-discover skills placed under `.claude/skills/<name>/` and `.codex/skills/<name>/`. Symlink or copy the skill folders in your target project:
  ```bash
  ln -s ../path/to/cloned/orchestune/skills/orchestune .claude/skills/orchestune
  ln -s ../path/to/cloned/orchestune/skills/orchestune .codex/skills/orchestune
  ```

* **Global Skill Directories**:
  If you want the skills to be available globally across all projects, place or symlink the skill folder under the agent's global skills directory:
  * **Claude Code**: `~/.claude/skills/orchestune/`
  * **Codex CLI**: `~/.codex/skills/orchestune/`
  * **Antigravity**: `~/.gemini/config/skills/orchestune/`

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

Before submission, Orchestune pushes the task branch to `origin`, then runs `codex cloud exec --env <environment_id> --branch <branch>` non-interactively. Completion is detected when an open PR has that branch as its head. If the environment ID is missing, Orchestune warns and safely falls back to the no-op target.

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

These run `claude -p "..." --permission-mode bypassPermissions` / `agy -p "..." --sandbox --dangerously-skip-permissions` / `codex exec "..." --dangerously-bypass-approvals-and-sandbox` (non-interactive print/exec mode) in each subtask's own worktree. All presets always pass a permission-bypass flag so an unattended run never blocks on an interactive prompt.

> [!IMPORTANT]
> **Trust Model and Security Risks**
> 
> These local CLI targets run with full permissions, bypassing interactive approvals and sandboxes. To prevent accidental unrestricted execution, you must explicitly opt in by passing the `--allow-unsafe-agent-execution` flag or setting `allow_unsafe_agent_execution = true` in your configuration file (e.g., `orchestune.toml`). If this option is not specified, Orchestune will fail to start (fail-closed).
> 
> Note that a dedicated `git worktree` is only a boundary for isolating source code changes; it is **not** an OS-level security boundary (sandbox). An agent process running with bypassed permissions can access anything the host user has access to, including your home directory, credentials, other repositories, and network resources. For untrusted codebases/issues, or when running in shared/production environments, we strongly recommend wrapping Orchestune in a secure container or VM isolation layer.

There is no separate permission-file setup step required; `orchestune bootstrap` only ensures the required GitHub labels exist.
