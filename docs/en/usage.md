# Usage & Command Reference

This document describes how to use the Orchestune CLI commands (`orchestune dag`, `orchestune dispatch`) and the specification for the task decomposition plan (`decomposition_plan.md`).

---

## 1. Task Decomposition Plan Specification

To split a main development task (a "big rock") into parallelizable subtasks, place a `decomposition_plan.md` file in the root of your repository.
This file consists of a YAML frontmatter section at the top for metadata and a markdown body below for descriptions.

### Example Format

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

### Frontmatter Schema
Each subtask item supports the following fields:

* **`id`** (string, required): A unique identifier for the subtask. Used for branch names and issue titles.
* **`description`** (string, required): A short description of what the task does.
* **`footprint`** (list of paths, required): Relative file paths (from the repository root) that this subtask is expected to create, modify, or delete.
* **`symbols`** (list of strings, optional): Function or class names that this subtask will define or modify.
* **`depends_on`** (list of strings, required): Subtask IDs that must be completed before this subtask can begin. Pass an empty array `[]` if there are no dependencies.

---

## 2. DAG Validation (orchestune-dag)

Validates that the tasks defined in `decomposition_plan.md` form a valid Directed Acyclic Graph (DAG) and have no conflicts.
While AI agents normally run this check automatically, you can also run it manually:

```bash
# Using the core CLI command
orchestune-dag --plan decomposition_plan.md

# Or using the wrapper command
orchestune dag --plan decomposition_plan.md
```

### Key Checks & Warnings
* **`DagCycleError`**: Raised if there is a circular dependency within `depends_on`.
* **File/Symbol Conflict**: Warnings or errors are output if multiple subtasks overlap in `footprint` or `symbols` without a defined dependency order.
* **Risk Flags**: Flags are set if potential security risks (credentials, subprocesses) are detected.

---

## 3. Running the Dispatcher (orchestune-dispatch)

Once the plan is finalized and approved, start the dispatcher to allocate subtasks to agents and begin development:

```bash
# Dry-run (preview execution plan without creating worktrees or updating labels)
orchestune-dispatch --no-apply

# Apply (run dispatch cycle: create worktrees, update labels, launch agents)
orchestune-dispatch
```

### Major Options

| Option | Default | Description |
| :--- | :--- | :--- |
| `--apply` / `--no-apply` | `--apply` | Choose whether to actually execute actions (worktree setup, API calls) or just preview them (dry-run). |
| `--max-concurrent <int>` | `2` | Maximum number of subtask agents running concurrently. |
| `--dispatch-target {local,cloud-routine,claude-cli}` | `local` | Target environment to launch agents. Run locally, dispatch to Claude Code Cloud Routine, or dispatch to a local `claude` CLI with a built-in preset command. |
| `--local-cmd <template>` | - | When using `--dispatch-target local`, a command template for dispatching to a local CLI (e.g. `agy`). Available placeholders: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}` (e.g. `agy --issue {issue_number}`). If omitted, the default dry-run stub command is used. With `--dispatch-target claude-cli`, this is optional and overrides the built-in `claude -p "..."` preset. |
| `--parent-issue <int>` | - | The parent GitHub Issue number that coordinates this plan. Created sub-issues will link to this parent. |
| `--deviation-buffer-lines <int>` | `50` | Allowed line modifications buffer outside the declared footprint to prevent live-locks. |
| `--max-launches-per-window <int>` | `10` | Rate limiting: maximum number of agent launches allowed in `--window-seconds`. |
| `--window-seconds <int>` | `3600` | The sliding window duration in seconds for launch rate-limiting (default is 1 hour). |

---

## 4. Integration & Auto-Rebase

The `orchestune-dispatch` command **handles both dispatching new tasks and integrating completed ones.**

1. Once an agent completes a task and opens a pull request (PR), the dispatcher detects it.
2. The dispatcher automatically creates a temporary integration branch and runs the local CI verification.
3. If the CI tests pass, it merges the PR into the `main` branch, and then automatically rebases any downstream active subtask branches to incorporate the latest changes, resolving conflicts early.
