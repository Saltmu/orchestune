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
    priority: high
    footprint:
      - src/db/connection.py
    symbols:
      - db.get_connection
    depends_on: []
    overview: "Provide the DB connection layer used across the app."
    acceptance_criteria:
      - "Connection pool initialization test passes"
    proposed_changes:
      - "Add get_connection to src/db/connection.py"
    verification_plan:
      - "poetry run pytest tests/test_connection.py"
    shared_contract: db-connection
    writes_shared_contract: true

  - id: user-auth
    description: "Implement user authentication endpoints"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
    shared_contract: db-connection
---
# Decomposition Plan Description
This plan outlines the steps required to build...
```

### Frontmatter Schema
Each subtask item supports the following fields:

* **`id`** (string, required): A unique identifier for the subtask. Used for branch names and issue titles.
* **`description`** (string, optional, defaults to `""`): A short description of what the task does. Used as input for risk detection.
* **`footprint`** (list of paths, optional, defaults to `[]`): Relative file paths (from the repository root) that this subtask is expected to create, modify, or delete.
* **`symbols`** (list of strings, optional, defaults to `[]`): Function or class names that this subtask will define or modify.
* **`depends_on`** (list of strings, optional, defaults to `[]`): Subtask IDs that must be completed before this subtask can begin. Pass an empty array `[]` if there are no dependencies (omitting the field means the same).
* **`priority`** (string, optional, defaults to `medium`): Subtask priority, one of `high` / `medium` / `low`. Any other value is not an error and is treated as `medium`. Affects the dispatch selection score.
* **`overview`** (string, optional, defaults to `""`): A longer description than `description`, copied into the "Overview" section of the created issue.
* **`acceptance_criteria`** (list of strings, optional, defaults to `[]`): Checklist items copied into the "Acceptance Criteria" section of the created issue.
* **`proposed_changes`** (list of strings, optional, defaults to `[]`): Items copied into the "Proposed Changes" section of the created issue.
* **`verification_plan`** (list of strings, optional, defaults to `[]`): Steps copied into the "Verification Plan" section of the created issue.
* **`risk`** (boolean, optional, defaults to `false`): Setting `true` explicitly flags the subtask as risky regardless of automatic detection (adding `explicit` to its risk reasons). Setting `false` does not disable automatic path/keyword based detection.
* **`shared_contract`** (string, optional, no default): A tag identifying a shared extension point such as a registry or CLI wiring. `orchestune-dag` warns when subtasks sharing the tag are not ordered relative to each other.
* **`writes_shared_contract`** (boolean, optional, defaults to `false`): Explicitly declares that this subtask writes to the `shared_contract` file. Usually unnecessary, since footprint matches are auto-detected.

> [!NOTE]
> `id` is the only required field. Parsing fails with an error if `id` is missing or blank.
> Every other field may be omitted and falls back to the default above. However, omitting `description` or `footprint`
> makes the parser emit a warning log, because risk detection and footprint conflict detection lose accuracy — specifying both is recommended in practice.

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
| `--dispatch-target {local,cloud-routine,codex-cloud,claude-cli,agy-cli,codex-cli,auto}` | auto-selected (non-CI: `auto` / GitHub Actions: `cloud-routine`) | Target environment to launch agents. When unspecified, it is auto-selected from the runtime environment (the `GITHUB_ACTIONS` variable). `auto` detects a local CLI on `PATH`. You can explicitly select a local CLI, Claude Code Cloud Routine, or a Codex Cloud environment configured through `ORCHESTUNE_CODEX_CLOUD_ENV` (or `--codex-cloud-env`). `codex-cloud` pushes the task branch to `origin`, submits it to Codex Cloud, and uses an open PR for that branch as the completion signal. Only explicitly passing `local` gives the backward-compatible no-op dummy (for tests/dry-runs). |
| `--codex-cloud-env <id>` | - | Codex Cloud environment ID used by `--dispatch-target codex-cloud`; defaults to `ORCHESTUNE_CODEX_CLOUD_ENV`. |
| `--local-cmd <template>` | - | When using `--dispatch-target local`, a command template for dispatching to a local CLI (e.g. `agy`). Available placeholders: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}` (e.g. `agy --issue {issue_number}`). If omitted, the default dry-run stub command is used. With `--dispatch-target claude-cli`/`agy-cli`/`codex-cli` (including when `auto` resolves to one of these), this is optional and overrides the built-in preset. |
| `--parent-issue <int>` | - | The parent GitHub Issue number that coordinates this plan. Created sub-issues will link to this parent. |
| `--deviation-buffer-lines <int>` | `5` | Allowed line modifications buffer outside the declared footprint to prevent live-locks. |
| `--max-launches-per-window <int>` | `1` | Rate limiting: maximum number of agent launches allowed in `--window-seconds`. |
| `--window-seconds <int>` | `3600` | The sliding window duration in seconds for launch rate-limiting (default is 1 hour). |
| `--max-recompute-retries <int>` | `2` | Maximum DAG recomputation retries after a footprint deviation is detected. Exceeding it falls back to forced serialization (force-serial). |
| `--run-state-path <path>` | `run_state.json` | Where the run state carried across dispatch cycles (active tasks, launch history) is persisted. |

### Configuration File for Omitting Options

You can place a configuration file in your project root directory to omit specifying options on the command line.

The dispatcher searches for configuration files in the following order and loads the first one found:
1. `orchestune.toml` in the project root.
2. `[tool.orchestune]` section in `pyproject.toml` in the project root.

#### Example Config (`orchestune.toml`)
```toml
max-concurrent = 2
dispatch-target = "claude-cli"
parent-issue = 181
run-state-path = "run_state.json"
```

#### Example Config (`pyproject.toml`)
```toml
[tool.orchestune]
max-concurrent = 2
dispatch-target = "claude-cli"
parent-issue = 181
run-state-path = "run_state.json"
```

> [!NOTE]
> Setting keys can be written in either kebab-case (e.g., `max-concurrent`) to match CLI options, or snake_case (e.g., `max_concurrent`) to match internal variables.
> If an option is explicitly specified as a command-line argument, it overrides the value in the configuration file.
> Unknown keys and invalid values stop startup with an error rather than falling back to defaults. Boolean settings must be TOML booleans, paths and string settings must be strings, and integer settings must be TOML integers. `max-concurrent`, `max-launches-per-window`, `deviation-buffer-lines`, and `max-recompute-retries` must be at least `0`; `window-seconds` and `parent-issue` must be at least `1`.

---

## 4. Integration & Auto-Rebase

The `orchestune-dispatch` command **handles both dispatching new tasks and integrating completed ones.**

### 4.1 The Common Integration Cycle

1. Once an agent completes a task, opens a pull request (PR), and the issue is labeled `status:done`, the dispatcher (Integrator) detects it.
2. The Integrator creates a temporary integration branch from the base branch (see below), merges the completed child branches into it one by one, and runs the local CI (`./scripts/local-ci.sh` by default).
3. If CI passes, it pushes the temporary integration branch to `origin` and creates (or reuses) an integration PR targeting the base branch.
4. Child issues that were included in the integration are labeled `integration:included`.

The base branch and the temporary integration branch depend on whether `--parent-issue` is given:

| `--parent-issue` | Base branch | Temporary integration branch |
| :--- | :--- | :--- |
| Given (`N`) | `origin/parent/issue-{N}` | `integration/temp-parent-issue-{N}` |
| Not given | `origin/main` | `integration/temp-main` |

### 4.2 With `--parent-issue` (two-tier integration via a parent branch)

When a parent issue number is given, integration is two-tiered: "child branches → parent branch" and "parent branch → main".

1. **Child branches → parent branch (automatic)**: Child PRs are automatically integrated into the `parent/issue-{N}` branch. Once the integration PR passes CI it is auto-merged without waiting for human approval, and the corresponding child issues are closed automatically. The individual child PRs opened by agents therefore do not need to be merged by a human; they remain as a review record.
   - If the auto-merge fails (branch protection, permissions, and so on), a comment is posted on the affected issues and the merge is retried automatically on the next dispatch cycle.
2. **Parent branch → main (merged by a human)**: Once every child issue under the parent is closed, a final integration PR from `parent/issue-{N}` to `main` is prepared automatically. **Deciding whether to merge that final PR, and performing the merge, is always done by a human.** When the final PR's merge is detected, the parent issue is closed automatically.

### 4.3 Without `--parent-issue`

Without a parent issue, the Integrator is responsible only up to creating an integration PR targeting `main`. **That integration PR is never auto-merged; a human reviews and merges it into `main`.**

### 4.4 Auto-Rebase

Downstream dependent task branches are rebased automatically depending on the state of the tasks they depend on. The rebase target is **the branch of the dependency whose PR has already passed CI** (stacking), not "the latest main". No auto-rebase happens when the dependency cannot be narrowed down to a single branch, or when the dependency has not passed CI yet.


