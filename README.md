# Orchestune

[English](README.md) | [日本語](README.ja.md)

Orchestune is a multi-agent implementation orchestrator designed to coordinate parallel development tasks. It automates DAG construction, scheduling, dispatch cycles, self-healing, and pull request integration.

Orchestune is provided as a **Skill for Agentic AI development** (e.g. Claude Code, Antigravity), allowing an AI agent to autonomously decompose tasks, dispatch subtasks, and integrate the results.

## Key Features

1. **DAG Construction & Conflict Prevention**
   - Automatically computes dependencies between subtasks.
   - Analyzes not only explicit dependencies (`depends_on`) but also overlap in target files (`footprint`) and code symbols (`symbols`) using similarity metrics, ensuring safe parallel execution without conflict.
   - Detects dependency cycles (`DagCycleError`) and highlights security/risk areas (e.g., credentials, subprocess commands).

2. **Intelligent Dispatch & Scheduling**
   - Schedules and launches subtasks in dedicated Git worktrees.
   - Limits the number of concurrent executions (`--max-concurrent`) and controls API burst rates (`--max-launches-per-window` within `--window-seconds`).
   - Supports local command execution and Claude Code Cloud Routine dispatch (`--dispatch-target`).

3. **Self-healing State Recovery**
   - Ideal for stateless CI/CD environments (like GitHub Actions) where local state files (`run_state.json`) may be lost.
   - Automatically reconstructs current execution states dynamically using active GitHub Issues and open PR branches.

4. **Integration & Rebase Coordination**
   - Monitors completion of subtasks (when PRs are opened).
   - Automates merging and rebasing downstream PR branches to minimize conflicts.
   - Integrates with semantic review workflows (LLM-based comments on integration PRs).

---

## Installation

Ensure you have Python 3.12+, Poetry, and the GitHub CLI (`gh auth status`) installed.

If you want your agent to run `orchestune-dag` / `orchestune-dispatch` inside a *different* project (e.g. a project called `manuscriptune`), set it up in two steps.

**Step A: Install the CLI**

```bash
# Globally, with pipx (recommended)
pipx install git+https://github.com/Saltmu/ochestune.git

# Or as a dev dependency of the target project (Poetry)
poetry add --group dev git+https://github.com/Saltmu/ochestune.git
```

This makes `orchestune-dag` and `orchestune-dispatch` runnable as plain commands from that project's directory.

**Step B: Point your agent at the skill definitions**

The agent needs to know the `orchestune` / `orchestune-dispatch` / `local-ci-developer` skills exist. Pick whichever your agent supports:

- **`.agents/skills.json`** (Antigravity): in the target project, add an entry pointing at this repo's `skills/` directory:
  ```json
  {
    "entries": [
      { "path": "../path/to/cloned/ochestune/skills" }
    ]
  }
  ```
- **Rule files** (Claude Code, Cursor, Codex CLI): copy the skill folder you need (e.g. `skills/orchestune/`) into the target project's own `skills/` directory, then add an entry in its `.clauderules` / `.cursorrules` / `.codexrules` (or `CLAUDE.md`) pointing at the copied `SKILL.md`. Use relative paths, not absolute local paths, when referencing the copied `SKILL.md`.
- **Global skill directory**: place or symlink the skill folder under your agent's global skills directory so it's available in every project without per-project setup (e.g. Claude Code: `~/.claude/skills/orchestune/`; check your agent's own docs for its equivalent path).

---

## Usage

A typical end-to-end flow looks like this:
1. You tell your agent: *"I want to implement this big feature (a 'big rock'). Decompose it with `orchestune` and set up parallel development."*
2. The agent auto-loads the `orchestune` skill in response.
3. The agent drafts `decomposition_plan.md` and validates the DAG using the installed CLI.
4. Once you approve the plan, the agent creates the GitHub Issues and kicks off the dispatcher.

### 1. Decomposition Plan & DAG Validation
To decompose your main task (a "big rock") into smaller subtasks, load the **`orchestune` core skill** in your agent (e.g. Claude Code, Antigravity). It will automatically survey the codebase, draft a `decomposition_plan.md`, validate the DAG, and create the corresponding GitHub Issues (with `Footprint` metadata and `status`/`priority` labels) for you — manually drafting a plan or creating issues by hand is rarely necessary.

For reference, `decomposition_plan.md` uses a YAML frontmatter format:

```markdown
---
subtasks:
  - id: setup-database
    description: "Initialize DB schemas and connection pool"
    footprint:
      - src/db/connection.py
    symbols:
      - db.get_connection
    depends_on: []

  - id: user-auth
    description: "Implement user authentication endpoints"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
---
# Decomposition Plan Description
This plan outlines the steps required to build...
```

The agent validates this plan itself, but you can also run the same check manually to inspect the DAG topology, circular dependencies, and risk flags:
```bash
orchestune-dag --plan decomposition_plan.md
# (or, inside this repo's own dev environment: poetry run orchestune-dag --plan decomposition_plan.md)
```

Issue creation follows a fixed convention (title format, `Footprint` YAML block, `status`/`priority`/`risk` labels) so the dispatcher can parse them — see [`skills/orchestune/SKILL.md`](skills/orchestune/SKILL.md) if you ever need to do this by hand.

### 2. Dispatcher Command
Run the scheduler/dispatcher:

```bash
# Dry-run (show plan without executing worktree creation or label updates)
orchestune-dispatch --no-apply

# Apply (run dispatch cycle: create worktrees, update labels, launch agents)
orchestune-dispatch
```

If you use `--dispatch-target cloud-routine`, set the `ORCHESTUNE_ROUTINE_ID` and `ORCHESTUNE_ROUTINE_TOKEN` environment variables first so the dispatcher can launch agents via Claude Code Cloud Routine.

This single command also covers **Integration & Rebase Coordination**: once a subtask's PR is opened, subsequent `orchestune-dispatch` runs detect it, rebase/merge it against downstream branches, and trigger semantic review — no separate command is needed.

#### Major Options:
- `--apply` / `--no-apply`: Actually execute actions or run in dry-run mode.
- `--max-concurrent <int>`: Maximum number of concurrently running subtasks.
- `--parent-issue <int>`: The parent GitHub Issue number coordinating the subtasks.
- `--dispatch-target {local,cloud-routine}`: Launch agent locally or via Claude Code Cloud Routine.
- `--deviation-buffer-lines <int>`: Allowed line changes buffer to avoid live-locks.

---

## Contributing

Want to develop Orchestune itself (run its test suite, local CI, etc.)? See [CONTRIBUTING.md](CONTRIBUTING.md).
