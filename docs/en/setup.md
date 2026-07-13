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

---

## 2. Registering Skills with AI Assistants

The AI agent needs to know that the `orchestune`, `orchestune-dispatch`, and `local-ci-developer` skills exist. Choose one of the following methods to register them:

### Method A: Automatic Setup (Recommended)
Run the setup command to automatically create symlinks in the global configuration directories of all supported AI assistants (Claude Code, Codex CLI, Antigravity):

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

Currently, the only supported cloud execution target for `--dispatch-target cloud-routine` is **Claude Code Cloud Routine**.

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

## 4. Setting Up Local `claude` / `agy` / `codex` CLI Dispatch

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

These run `claude -p "..." --permission-mode bypassPermissions` / `agy -p "..." --sandbox --dangerously-skip-permissions` / `codex exec "..." --dangerously-bypass-approvals-and-sandbox` (non-interactive print/exec mode) in each subtask's own worktree. All presets always pass a permission-bypass flag so an unattended run never blocks on an interactive prompt — the subtask's dedicated `git worktree` is the safety boundary, the same pattern used by cloud-based agent orchestrators. There is no separate permission-file setup step required; `orchestune bootstrap` only ensures the required GitHub labels exist.
