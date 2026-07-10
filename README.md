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
   - Supports local command execution and Claude Code Cloud Routine dispatch (`--dispatch-target`). Claude Code Cloud Routine is currently the only supported cloud target (support for other backends, such as Codex Cloud, is planned for the future).

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
- **Project skills** (Claude Code, Codex CLI): both agents natively auto-discover skills placed under `.claude/skills/<name>/` and `.codex/skills/<name>/` respectively (`SKILL.md` is a cross-agent format, so the same file works for both). In the target project, copy or symlink the skill folder there, e.g.:
  ```bash
  ln -s ../path/to/cloned/ochestune/skills/orchestune .claude/skills/orchestune
  ln -s ../path/to/cloned/ochestune/skills/orchestune .codex/skills/orchestune
  ```
  This repo does the same for its own skills — see `.claude/skills/` and `.codex/skills/` for a working example.
- **Global skill directory**: place or symlink the skill folder under your agent's global skills directory so it's available in every project without per-project setup (e.g. Claude Code: `~/.claude/skills/orchestune/`; Codex CLI: `~/.codex/skills/orchestune/`).

---

## Usage

`orchestune` is the **only skill you ever need to invoke directly** — it drives everything else internally. A typical end-to-end flow looks like this:
1. You tell your agent: *"I want to implement this big feature (a 'big rock'). Decompose it with `orchestune` and set up parallel development."*
2. The agent auto-loads the `orchestune` skill in response.
3. `orchestune` asks the `orchestune-dag` CLI to build/validate the DAG for a drafted `decomposition_plan.md`, and presents the result to you.
4. You approve the plan, or give feedback — `orchestune` revises the plan and re-validates until you approve.
5. Once approved, `orchestune` hands off internally to the `orchestune-dispatch` skill, which creates the GitHub Issues and configures/starts the dispatcher. You never need to load `orchestune-dispatch` yourself.

### 1. Decomposition Plan & DAG Validation
To decompose your main task (a "big rock") into smaller subtasks, load the **`orchestune` core skill** in your agent (e.g. Claude Code, Antigravity). It will automatically survey the codebase, draft a `decomposition_plan.md`, validate the DAG, iterate with you on approval, and then hand off to `orchestune-dispatch` to create the corresponding GitHub Issues (with `Footprint` metadata and `status`/`priority` labels) — manually drafting a plan or creating issues by hand is rarely necessary.

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

Issue creation follows a fixed convention (title format, `Footprint` YAML block, `status`/`priority`/`risk` labels) so the dispatcher can parse them — see [`skills/orchestune-dispatch/SKILL.md`](skills/orchestune-dispatch/SKILL.md) if you ever need to do this by hand.

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

#### Setting Up a Claude Code Cloud Routine

Right now, the only cloud target `--dispatch-target cloud-routine` supports is **Claude Code Cloud Routine** (support for other cloud agent backends, such as Codex Cloud, is planned for the future).

1. Open [claude.ai/code/routines](https://claude.ai/code/routines) and click **New routine**. The prompt body can be minimal — the dispatcher sends the actual task instructions as `text` on every `fire` call.
2. Under **Repositories**, add the GitHub repository you want to dispatch against (the routine clones it from the default branch on every run).
3. Under **Select a trigger**, click **Add another trigger**, choose **API**, then save the routine.
4. After saving, the same screen shows the URL (`https://api.anthropic.com/v1/claude_code/routines/<routine_id>/fire`) — note the `routine_id` from it — and a **Generate token** button. The token is shown only once, so copy it somewhere safe immediately.
5. Set the `routine_id` and token as environment variables:
   ```bash
   export ORCHESTUNE_ROUTINE_ID="<routine_id>"
   export ORCHESTUNE_ROUTINE_TOKEN="<token>"
   ```

The dispatcher always generates branch names in the `claude/issue-<issue-number>-<subtask-id>` format, which already matches the routine's default branch-push restriction (only `claude/`-prefixed branches are allowed), so there's no need to enable "Allow unrestricted branch pushes."

For further details (token rotation/revocation, network access restrictions, etc.), see the [official Claude Code documentation on Routines](https://code.claude.com/docs/en/routines.md).

#### Major Options:
- `--apply` / `--no-apply`: Actually execute actions or run in dry-run mode.
- `--max-concurrent <int>`: Maximum number of concurrently running subtasks.
- `--parent-issue <int>`: The parent GitHub Issue number coordinating the subtasks.
- `--dispatch-target {local,cloud-routine}`: Launch agent locally or via Claude Code Cloud Routine.
- `--deviation-buffer-lines <int>`: Allowed line changes buffer to avoid live-locks.

---

## Contributing

Want to develop Orchestune itself (run its test suite, local CI, etc.)? See [CONTRIBUTING.md](CONTRIBUTING.md).
