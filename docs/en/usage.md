# Usage & Command Reference

This document describes how to use the Orchestune CLI commands (`orchestune dag`, `orchestune provision`, `orchestune dispatch`) and the specification for the task decomposition plan (`decomposition_plan.md`).

---

## 1. Task Decomposition Plan Specification

To split a main development task (a "big rock") into parallelizable subtasks, place a `decomposition_plan.md` file in the root of your repository.
This file consists of a YAML frontmatter section at the top for metadata and a markdown body below for descriptions.

### Example Format

```markdown
---
title: "One-line summary of the 'big rock' itself"
parent_issue_number: null  # filled in by `orchestune provision` once the parent issue exists (or pre-set if decomposing an existing issue)
parent_issue_source: derived  # "adopted" when adopting an existing issue, "derived" when creating a new EPIC
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
    issue_number: null  # filled in by `orchestune provision` once this subtask's issue exists

  - id: user-auth
    description: "Implement user authentication endpoints"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
    shared_contract: db-connection
    issue_number: null
---
# Decomposition Plan Description
This plan outlines the steps required to build...
```

### Frontmatter Schema

The top level supports the following fields:

- **`title`** (string, required): A one-line summary of the "big rock" as a whole. `orchestune provision` (see below) uses it to create the parent issue (`[EPIC] <title>`).
- **`parent_issue_number`** (integer or `null`, optional, defaults to `null`): The parent issue's number. When decomposing an existing issue, set this to that issue's number. Otherwise, `orchestune provision` writes it back after creating (or reusing) the parent issue.
- **`parent_issue_source`** (string, optional, defaults to `derived`): Provenance of the parent issue: either `adopted` (adopted a pre-existing issue as parent) or `derived` (created/resolved from the plan's `title`). When `adopted`, title matching is bypassed and the issue is verified and reused based on the issue number and parent marker.
- **`subtasks`** (list of subtasks, required): Each item supports the following fields.

Each subtask item supports the following fields:

* **`id`** (string, required): A unique identifier for the subtask. Used for branch names and issue titles. It must be a string: YAML numbers, booleans, dates, nulls, and lists (e.g. `id: 123`, `id:`, `id: []`) are rejected with an error. Quote the value (`id: "123"`) if you need a numeric-looking ID.
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
* **`shared_contract`** (string, optional, no default): A tag identifying a shared extension point such as a registry or CLI wiring. `orchestune-dag` only compares subtasks judged to actually **write** to the shared file; pure consumers (subtasks that merely `depends_on` the contract and only read/import it) are excluded. Writer pairs become exclusions in the Conflict Graph, and an additional warning appears when they are not ordered in the Precedence DAG (neither is reachable from the other).
* **`writes_shared_contract`** (boolean, optional, defaults to `false`): Declares that this subtask writes to the `shared_contract` file. Writer status is first auto-detected by matching `footprint` paths against these filename categories:
    * `registry`: filenames containing `registry` / `registration` / `registrar` (e.g. `src/format_registry.py`)
    * `cli-wiring`: `cli.*` / `__main__.*` / `main.*`
    * `public-api`: `__init__.py` / `index.ts` / `index.js` / `index.tsx` / `index.jsx`
    * `dependency-manifest`: `pyproject.toml` / `package.json` / `poetry.lock` / `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml` / `Cargo.toml` / `go.mod`

    Auto-detection does not apply to custom filenames outside those categories (e.g. `src/db/connection.py`, `src/custom_hook.py`), so for those you **must set `writes_shared_contract: true`**. Omitting it makes both subtasks count as consumers even when they share the same `shared_contract` tag, and no warning is emitted at all.
* **`execution_profile`** (string or `null`, optional, defaults to `null`): An abstract execution profile name for the agent executing this subtask (e.g. `fast-code`, `deep-reasoning`). Must be up to 32 characters consisting of lowercase alphanumeric characters, hyphens, and underscores.
* **`model_tier`** (string or `null`, optional, defaults to `null`): Abstract model capability tier assigned to the subtask (`weak` / `middle` / `strong`). Automatically resolved to a concrete model name for each dispatch target (`claude-cli`, `codex-cli`, `agy-cli`, etc.) based on `[model_tiers]` in `orchestune.toml` or built-in defaults. If specified, the model from `model_tier` overrides any model configured in the `execution_profile` while preserving `reasoning_effort`. Values other than `weak` / `middle` / `strong` are rejected with an error.
* **`issue_number`** (integer or `null`, optional, defaults to `null`): This subtask's issue number. **Do not set this by hand** — `orchestune provision` writes it back after creating (or reusing) this subtask's issue. If it is already set, `orchestune provision` reuses that issue instead of creating a new one.

### Plan Lifecycle and Parent Issue Persistence (Option b)

`decomposition_plan.md` acts as a local draft/working file during the drafting, DAG validation, and user review stages (Stages 1–3).
When `orchestune provision` (Stage 4) runs, the parent (EPIC) issue is created or adopted, and the latest plan contents (Frontmatter YAML) are automatically embedded and synchronized into the parent issue body within an `<!-- orchestune:decomposition-plan -->` block.

- **Parent Issue as Source of Truth**: Even if an AI agent's disposable worktree is removed and the local `decomposition_plan.md` is lost, the entire plan (including all subtask definitions, resolved `issue_number`s, and prose description) remains safely persisted in the parent issue body on GitHub.
- **Safe Recovery from Lost Plan Files**:
  1. Run `orchestune provision --restore-plan <parent_number>` (and optionally `--plan <output_path>`) to automatically restore `decomposition_plan.md` (frontmatter and original prose description) directly from the parent issue body.
  2. If re-running `orchestune provision` after restoring the plan file, specify `--parent-issue <parent_number>` and first run with `--no-apply` to preview and confirm that existing child issues will be reused.
- **Concurrent Big Rocks**:
  To manage multiple big rocks in parallel, specify separate plan paths (e.g. `orchestune provision --plan plans/rock-a.md`) or manage them in isolated worktrees. Since each big rock's plan is persisted directly in its corresponding parent issue body, they remain cleanly separated and never conflict.
- **`orchestune-dispatch` Does Not Read Plan Files**:
  `orchestune-dispatch` reconstructs the Precedence DAG and Conflict Graph exclusively from the Footprint YAML blocks embedded within the child GitHub Issue bodies (`subtask_id`, `depends_on`, `footprint`, `symbols`, `shared_contract`, `writes_shared_contract`, etc.). It never reads `decomposition_plan.md`. Therefore, dispatching, parallel execution, self-healing, and merge integration remain completely intact even if the local plan file is absent.

> [!NOTE]
> `id` is the only required field. Parsing fails with an error if `id` is missing or blank.
> Every other field may be omitted and falls back to the default above. However, omitting `description` or `footprint`
> makes the parser emit a warning log, because risk detection and footprint conflict detection lose accuracy — specifying both is recommended in practice.

> [!NOTE]
> `orchestune provision`'s issue-number write-back (`parent_issue_number` and each subtask's `issue_number`) assumes **standard block-style YAML** for the `subtasks:` list, as shown in the format example above — each subtask spelled out across multiple `- key: value` lines with unquoted, bare-identifier keys. Single-line flow-style mappings (`- {id: task-a, ...}`) are also supported, but non-standard forms — a flow mapping split across multiple physical lines, or quoted keys (`"id": task-a`) — are not guaranteed to work. Write approved plans using the standard forms shown above.

---

## 2. Provisioning Issues (orchestune provision)

Files GitHub Issues from an approved `decomposition_plan.md`: `title` becomes the parent issue, and each subtask becomes a child issue (sub-issue). Issue bodies are rendered from `.github/issue_template.md`'s placeholder rules, subtasks are filed in `depends_on` topological order, and the native parent/blocked-by relationships are set via `--parent`/`--blocked-by`-equivalent operations. Every resolved issue number is written back into `decomposition_plan.md`'s frontmatter (`parent_issue_number`, each subtask's `issue_number`) as soon as it is known, and the complete plan YAML is synchronized into the parent issue's `<!-- orchestune:decomposition-plan -->` body block. This makes the command **idempotent** (a subtask that already has an issue is never recreated) and **resumable after a partial failure** (if subtask N fails, re-running does not duplicate subtasks 1..N-1).

```bash
# Preview only (nothing is written to GitHub; prints the generated body/labels)
orchestune provision --plan decomposition_plan.md --no-apply

# Actually file the issues
orchestune provision --plan decomposition_plan.md
```

### Major Options

| Option | Default | Description |
| :--- | :--- | :--- |
| `--plan <path>` | `decomposition_plan.md` | Path to the decomposition plan to provision from. |
| `--template <path>` | `.github/issue_template.md` | Path to the issue body template. |
| `--apply` / `--no-apply` | `--apply` | Choose whether to actually create issues on GitHub and write back numbers, or just preview them (dry-run). |
| `--parent-issue <number>` | none | Attach subtasks to this existing issue as their EPIC parent, instead of creating/reusing one derived from `title`. See "Attaching to a pre-existing EPIC issue" below. |

### Attaching to a pre-existing EPIC issue (`--parent-issue`)

If the EPIC issue was already filed ahead of time (by hand, or via plain GitHub — not by Orchestune), either specify `parent_issue_number: <number>` and `parent_issue_source: adopted` in the plan frontmatter, or pass `--parent-issue <number>` on the command line:

```bash
orchestune provision --plan decomposition_plan.md --parent-issue 123
```

If the target issue doesn't already look like an Orchestune EPIC (title starting with `[EPIC] ` and the parent marker embedded in the body), it is normalized in place — its existing content is preserved, and the `[EPIC] ` prefix / parent marker are added as needed. No title match against the `title` frontmatter field is required.

Running with `--parent-issue` automatically persists `parent_issue_source: adopted` into `decomposition_plan.md`'s frontmatter. **Subsequent `orchestune provision` runs therefore no longer require `--parent-issue` to be passed again**; they will automatically reuse the adopted parent and existing child issues. If an adopted parent issue does not exist, provisioning halts with an error instead of silently creating a duplicate parent.

> [!NOTE]
> When running `orchestune-dispatch`, continue to pass `--parent-issue <number>` to enable two-tier merge integration into the parent branch (`parent/issue-<number>`).

### Provisioning Rules

* **Labels**: `status:queued` if `depends_on` is empty or every dependency is already `status:done`; otherwise `status:blocked`. `priority:high`/`medium`/`low` follows `priority`; `risk: true` adds `risk:flagged`.
* **Idempotency check order**: (1) reuse the subtask's `issue_number` if already set; (2) otherwise search the parent's existing child issues for one whose body embeds a matching `subtask_id` in its Footprint YAML block, and reuse it if found; (3) only create a new issue if neither matches.
* Requires the `gh` CLI to be installed and authenticated (`orchestune bootstrap` verifies this beforehand). See the [orchestune-provision skill](../../skills/orchestune-provision/SKILL.md) for the fallback procedure when `gh` is unavailable.

---

## 3. DAG Validation (orchestune-dag)

Builds a Precedence DAG from explicit `depends_on` declarations, validates that it is acyclic, and separately displays the symmetric Conflict Graph inferred from `footprint`, `symbols`, and shared-contract metadata.
While AI agents normally run this check automatically, you can also run it manually:

```bash
# Using the core CLI command
orchestune-dag --plan decomposition_plan.md

# Or using the wrapper command
orchestune dag --plan decomposition_plan.md
```

### Major Options

| Option | Default | Description |
| :--- | :--- | :--- |
| `--threshold <float>` | - | Similarity-conflict threshold in `[0, 1]`. When omitted, falls back to the `dag_similarity_threshold` config-file setting (see below) if set, otherwise to `0.2` (`orchestune.dag.similarity.DEFAULT_SIMILARITY_THRESHOLD`). Values outside `[0, 1]` (including `nan`/`inf`) are rejected with an error. |

### Configuration File Options

Like `orchestune-dispatch` (§4), `orchestune-dag` also reads `orchestune.toml` / `pyproject.toml`'s `[tool.orchestune]` table (same discovery order: `orchestune.toml` first, then `pyproject.toml`).

| Setting | Default | Description |
| :--- | :--- | :--- |
| `dag_ignore_patterns` (or `dag-ignore-patterns`) | `[]` | List of regex strings matched only against `footprint` paths; `symbols` always remain in the similarity-scoring input. A matching path is excluded, in addition to the built-in ignore list (`pyproject.toml`, `poetry.lock`, `logging.py`, `logger.py`, `config.py`, `settings.py`), from similarity Conflict Edge scoring and heuristic shared-contract-hotspot conflicts. A pair can still conflict through another unignored path or a shared `symbols` entry. Explicit `shared_contract` writer conflicts and the independent writer warning are unaffected. The Precedence DAG contains only explicit `depends_on` edges, so this setting cannot affect `DagCycleError`. Empty strings are rejected because they match every path. |
| `dag_similarity_threshold` (or `dag-similarity-threshold`) | `0.2` | Persisted fallback for `--threshold` (see above), a float in `[0, 1]`. Also read by `orchestune provision`'s own Conflict Graph computation from the same config file, so a threshold tuned here isn't silently ignored there. Note: both `orchestune-dag` and `orchestune provision` resolve the repository root via the shared `resolve_repo_root()` helper, which walks up to the enclosing Git repository — so even when `--plan` points to a file nested below the repository root, both tools locate the same repository-root config consistently. |

#### Example Config (`orchestune.toml`)

```toml
dag_ignore_patterns = ['(^|/)package\.json$', '(^|/)generated/']
dag_similarity_threshold = 0.35
```

> [!WARNING]
> `dag_ignore_patterns` entries are regular expressions read from TOML, not literal path fragments. Prefer TOML **literal strings** (single quotes, `'...'`) as shown above: backslashes are taken verbatim, so `\.` is written exactly as the regex intends.
> If you use a TOML **basic string** (double quotes, `"..."`) instead, the backslash is *also* a TOML escape character, so every regex backslash needs its own escape — a regex `\.` must be written `"\\."`. `"(^|/)package\\.json$"` (basic string) and `'(^|/)package\.json$'` (literal string) compile to the exact same regular expression. A bare `"\."` inside a basic string is rejected by the TOML parser as an invalid escape sequence, not merely as "the wrong regex".

### Key Checks & Warnings
A single `Warnings:` output can combine more than one of the warning types below at once — check each entry against its own wording rather than assuming they're all the same kind.
* **`DagCycleError`**: Raised if there is a circular dependency within `depends_on`.
* **Conflict edges**: Similarity across `footprint` / `symbols` and shared-contract writer detection produce symmetric exclusions independent of priority or ID. Text output separates `Precedence edges:` from `Conflict edges:`; `--json` separates `precedence_edges` from `conflict_edges` (the compatibility `edges` key contains precedence only).
* **Shared-contract writer warning**: A non-blocking warning accompanies the Conflict Edge when writers are not ordered in the Precedence DAG.
* **Existence Verification (`footprint`/`symbols`)**: Warns when a declared `footprint` path or `symbols` entry cannot be confirmed to exist in the current codebase (e.g. `<subtask-id>: footprintに実在しないパスがあります` / `<subtask-id>: symbolsが実コードベースに見つかりません`). This is not necessarily an error — a `footprint` path about to be created for the first time is always reported this way, but a not-yet-existing `symbols` entry is only reported when verification actually ran, which requires an existing, successfully-parsed `.py` file in the footprint *and* no unparseable existing `.py` file anywhere in it (even one file with a syntax/encoding error means verification is silently skipped for the whole subtask). When verification didn't run, no `symbols` warning appears at all — that silence does not mean the symbol was confirmed. See the [`orchestune` skill](../../skills/orchestune/SKILL.md)'s Stage 2 for the full triage guidance (typo/wrong path vs. a missed `footprint` declaration).
* **Risk Flags**: Flags are set if potential security risks (credentials, subprocesses) are detected.

---

## 4. Running the Dispatcher (orchestune-dispatch)

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
| `--dispatch-target {local,cloud-routine,codex-cloud,claude-cli,agy-cli,codex-cli,auto}` | auto-selected (non-CI: `auto` / GitHub Actions: `cloud-routine`) | Target environment to launch agents. When unspecified, it is auto-selected from the runtime environment (the `GITHUB_ACTIONS` variable). `auto` detects a local CLI on `PATH`. You can explicitly select a local CLI, Claude Code Cloud Routine, or a Codex Cloud environment configured through `ORCHESTUNE_CODEX_CLOUD_ENV` (or `--codex-cloud-env`). `codex-cloud` pushes the task branch to `origin`, submits it to Codex Cloud, and combines Cloud task tracking with branch PR / outcome record status to determine completion. Only explicitly passing `local` gives the backward-compatible no-op dummy (for tests/dry-runs). |
| `--reviewer-bot {auto,claude,codex}` | `auto` | Reviewer requested after implementation. `auto` is evaluated after the dispatch target is resolved and selects a cross-vendor reviewer: Claude targets use Codex; Codex and `agy` targets use Claude. An explicit value overrides this mapping. Generic `local` cannot be inferred and emits a warning. |
| `--consistency-mode {off,shadow,repair}` | `off` | Additional repository-wide consistency loop. `off` keeps established behavior, `shadow` adds reports without new mutations, and `repair` can execute explicitly user-allowlisted codes. Built-in safe self-healing remains enabled in every mode. |
| `--consistency-repair-code <code>` | - | Finding or command code allowed in the additional `repair` loop. Repeat the option for multiple codes. An empty user allowlist is report-only and does not disable built-in safe repairs. |
| `--consistency-max-repair-passes <1..5>` | `1` | Maximum guarded repair/re-observation passes per dispatch cycle. The same idempotency key is not executed twice in one cycle. |
| `--codex-cloud-env <id>` | - | Codex Cloud environment ID used by `--dispatch-target codex-cloud`; defaults to `ORCHESTUNE_CODEX_CLOUD_ENV`. |
| `--local-cmd <template>` | - | When using a local target, a command template for dispatching to a CLI. Available placeholders: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}`, `{model}`, `{reasoning_effort}`, `{profile}`, and `{reviewer_bot}`. If omitted for generic `local`, the dry-run stub is used. With a local CLI preset, this option replaces the preset; Orchestune substitutes `{reviewer_bot}` when present but never appends review instructions to arbitrary custom commands. |
| `--parent-issue <int>` | - | The parent GitHub Issue number that coordinates this plan. Created sub-issues will link to this parent. |
| `--ci-command <cmd>` | `./scripts/local-ci.sh` (specific to Orchestune's own repository) | The CI command the Integrator runs on the integration branch (a shell-like string parsed with shlex, e.g. `'make ci'`). Set this explicitly if your repository's CI entrypoint differs (see [Setup Guide § Prerequisites](setup.md#0-prerequisites)). In `orchestune.toml`/`pyproject.toml`'s `[tool.orchestune]` section, use the `ci-command` key. |
| `--deviation-buffer-lines <int>` | `5` | Allowed line modifications buffer outside the declared footprint to prevent live-locks. |
| `--max-launches-per-window <int>` | `1` | Rate limiting: maximum number of agent launches allowed in `--window-seconds`. |
| `--window-seconds <int>` | `3600` | The sliding window duration in seconds for launch rate-limiting and token quotas (default is 1 hour). |
| `--max-tokens-per-window <int>` | - | Quota limit: maximum total tokens consumed across completed tasks within `--window-seconds`. When reached, new task launches are paused. Unlimited if omitted. |
| `--max-tokens-per-task <int>` | - | Per-task limit: maximum token consumption allowed for a single subtask. If exceeded upon completion, automatic completion is halted and escalated to `status:blocked-human-review`. Unlimited if omitted. |
| `--max-recompute-retries <int>` | `2` | Maximum runtime Conflict Graph recomputation retries after a footprint deviation is detected. Exceeding it falls back to forced serialization (force-serial). |
| `--task-timeout-seconds <int>` | `0` | Seconds after which a running task is treated as timed out and reclaimed by the GC. `0` (the default) disables timeout reclamation and only detects zombies. Set a positive value before leaving a run unattended. |
| `--max-task-reclaims <int>` | `3` | Maximum number of times the zombie/timeout GC may return the same task to `status:queued`. Once exceeded, the task moves to `status:blocked-human-review` and is no longer requeued. `0` means the very first reclaim escalates; there is no value that makes it unlimited. |
| `--early-death-window-seconds <int>` | `120` | Treat a no-commit local process exit within this many seconds of launch as a transient startup failure. `0` restricts this recovery to an immediate exit. |
| `--max-early-death-retries <int>` | `2` | Maximum automatic requeues for transient startup failures. The next no-commit exit escalates to `status:blocked-human-review`. |
| `--early-death-backoff-seconds <int>` | `60` | Base delay for an early-death requeue. Each retry doubles the previous delay. |
| `--not-needed-review-timeout-seconds <int>` | `86400` | Maximum number of seconds a pending `status:not-needed` independent review (Cloud Routine target only) is kept without either outcome label appearing. An entry past the limit escalates to `status:blocked-human-review`; there is no value that makes it unlimited. |
| `--model <name>` | - | Override the concrete model name to use at runtime (e.g. `claude-3-7-sonnet`, `o3-mini`, `gemini-2.5-pro`). When omitted, follows profile/tier settings. |
| `--reasoning-effort <effort>` / `--effort <effort>` | - | Override the reasoning effort at runtime (e.g. `low`, `medium`, `high`). When omitted, follows profile settings. |
| `--run-state-path <path>` | `run_state.json` | Where the run state carried across dispatch cycles (active tasks, launch history) is persisted. |

The default self-healing allowlist is intentionally separate from `--consistency-repair-code`. It contains `status.blocked-with-resolved-dependencies`, `status.primary-status-conflict`, `execution.requeue`, `execution.update-bookkeeping`, and `execution.reclaim`, preserving the status promotion/reconciliation, state recovery, and GC behavior that predates the optional loop. Codes that reached a built-in repair pass are not attempted again by the later repository-wide repair loop; commands that appeared only as planner candidates remain eligible for the user allowlist. Opted-in execution commands use the same guarded GC and recovery handlers as the built-in boundaries.

Use `off` for unchanged behavior, `shadow` to inspect additional start/end findings, `repair` with no repair codes to inspect final dispositions without enabling a new policy, and then a limited set of `--consistency-repair-code` options to opt in. `--apply` permits the established repairs and opted-in policies to mutate; `--no-apply` permits no external or durable repair side effects (GC output is a preview and recovery may update only ephemeral in-memory preview bookkeeping).

Inspect `consistency.scans`, `consistency.repair_passes`, and `consistency.repair_outcomes` in `--json` output or `events.jsonl`. Outcomes are `resolved`, `unresolved`, `deferred`, `failed`, or `observation-unknown`. Unknown/stale observations and non-repairable findings remain visible without being mutated. A skipped command-level result caused by dry-run or a failed live precondition is represented by the finding's final disposition; there is no fallback to an old phase-owned repair path. A failed partial status transition leaves its Intent journal beside `run_state.json` so the next cycle can resume it without duplicating the external side effect.

### Configuration File for Omitting Options

You can place a configuration file in your project root directory to omit specifying options on the command line (a fully-commented template `orchestune.toml.example` is available in the repository root).

The dispatcher searches for configuration files in the following order and loads the first one found:
1. `orchestune.toml` in the project root.
2. `[tool.orchestune]` section in `pyproject.toml` in the project root.

#### Example Config (`orchestune.toml`)
```toml
max-concurrent = 2
dispatch-target = "claude-cli"
reviewer-bot = "auto"
consistency-mode = "shadow"
consistency-repair-code = []
consistency-max-repair-passes = 1
parent-issue = 181
run-state-path = "run_state.json"
default_execution_profile = "balanced"

[execution_profiles.balanced.claude-cli]
model = "claude-3-5-haiku-20241022"

[execution_profiles.balanced.codex-cli]
model = "gpt-4o-mini"
reasoning_effort = "low"

[execution_profiles.deep-reasoning.claude-cli]
model = "claude-3-7-sonnet-20250219"

[execution_profiles.deep-reasoning.codex-cli]
model = "o3-mini"
reasoning_effort = "high"

[execution_profiles.deep-reasoning.cloud-routine]
model = "claude-3-7-sonnet-20250219"

[execution_profiles.fast-code.claude-cli]
model = "claude-3-5-sonnet-20241022"

[execution_profiles.fast-code.codex-cli]
model = "gpt-4o"
```

#### Example Config (`pyproject.toml`)
```toml
[tool.orchestune]
max-concurrent = 2
dispatch-target = "claude-cli"
reviewer-bot = "auto"
consistency-mode = "shadow"
consistency-repair-code = []
consistency-max-repair-passes = 1
parent-issue = 181
run-state-path = "run_state.json"
default_execution_profile = "balanced"

[tool.orchestune.execution_profiles.balanced.claude-cli]
model = "claude-3-5-haiku-20241022"

[tool.orchestune.execution_profiles.balanced.codex-cli]
model = "gpt-4o-mini"
reasoning_effort = "low"

[tool.orchestune.execution_profiles.deep-reasoning.claude-cli]
model = "claude-3-7-sonnet-20250219"

[tool.orchestune.execution_profiles.deep-reasoning.codex-cli]
model = "o3-mini"
reasoning_effort = "high"
```

> [!NOTE]
> Setting keys can be written in either kebab-case (e.g., `max-concurrent`) to match CLI options, or snake_case (e.g., `max_concurrent`) to match internal variables.
> If an option is explicitly specified as a command-line argument, it overrides the value in the configuration file.
> Unknown keys and invalid values stop startup with an error rather than falling back to defaults. Boolean settings must be TOML booleans, paths and string settings must be strings, and integer settings must be TOML integers. `consistency-repair-code` must be a list of non-empty strings. `max-concurrent`, `max-launches-per-window`, `deviation-buffer-lines`, `max-recompute-retries`, `task-timeout-seconds`, `max-task-reclaims`, `early-death-window-seconds`, `max-early-death-retries`, `early-death-backoff-seconds`, and `not-needed-review-timeout-seconds` must be at least `0`; `window-seconds` and `parent-issue` must be at least `1`, and `consistency-max-repair-passes` must be between `1` and `5`.
>
> In `[execution_profiles]` (or `[tool.orchestune.execution_profiles]`), define target-specific tables (`claude-cli`, `agy-cli`, `codex-cli`, `cloud-routine`, `codex-cloud`) under each profile name (e.g. `balanced`, `deep-reasoning`, `fast-code`). Each target configuration accepts `model` (string) and `reasoning_effort` (`"low"` / `"medium"` / `"high"`). When defining the `execution_profiles` table, the entry corresponding to `default_execution_profile` (defaults to `"balanced"`) must be included.

---

## 5. Integration & Auto-Rebase

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

### 4.5 Issue ↔ PR link notices

GitHub's `Closes #N` auto-linking and the "Development" sidebar on an issue only work when the PR targets the default branch (`main`). Under the parent-branch workflow that leaves a child issue with no visible trace of the PR that implemented it, so Orchestune fills the gap with comments:

1. **When a PR is opened**: once the dispatcher sees an open PR, it posts a "PR #XXX has been opened" notice on the corresponding child issue. The target issue is resolved both from the PR's `Closes #N` references and from the head branch name (`claude/issue-{N}-{subtask_id}`), so PRs opened by the agent itself are announced through the same path. A notice is posted **only when the PR's base is exactly that issue's own parent branch** (`parent/issue-{parent_issue_number}`), so a PR targeting a different parent branch that merely references the issue is ignored. The PR's head must also live in the upstream repository: PRs from forks, and any PR whose head origin cannot be confirmed, are skipped so that a third party cannot post an authoritative-looking notice on someone else's issue.
2. **When a PR is merged**: when the Integrator merges the integration PR into the parent branch and closes the child issue, it posts a "PR #XXX has been merged into the parent branch" completion notice *just before* closing. The notice is deliberately not folded into the closing comment: if only the close fails, the retry on the next cycle can no longer recover the integration PR number, and the link would be lost for good.

Both comments embed a `<!-- orchestune:pr-link:{created|merged}:{pr_number} -->` marker, so the same notice is never posted twice. The two notices differ in how they handle a failure to read the existing comments: the creation notice is skipped and retried on the next cycle, while the merge notice is posted anyway, because it is the last write before the issue is closed and would otherwise never be retried.
