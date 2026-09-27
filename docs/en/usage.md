# Usage & Command Reference

### Cloud launch recovery

Ordinary `cloud-routine` and `codex-cloud` worker launches persist an
`orchestune:launch-attempt` JSON block in the task Issue before dispatch.
The journal is independent of the parent Issue's quota timestamps:

| Phase at interruption | Recovery |
|---|---|
| No journal | No provider call was authorized; start a new attempt. |
| `prepared` | The provider has not been called; resume the same attempt ID. |
| `unknown` | The call may have succeeded. Look up the exact attempt if supported; otherwise hold for human review. Never replay it as a queued task. |
| `launched` | The handle is durable. Restore the same ID, phase, start time, branch and handle even without a PR or local state. |

The built-in cloud providers do not currently offer attempt-ID lookup or
idempotent launch through these adapters. Normal Routine worker POSTs therefore
run once, without transport retries. A failure after the provider boundary keeps
the worktree and quota reservation. Local workers and Semantic Review's
`fire_text` retry contract are unchanged.

An unresolved attempt transitions to `status:blocked-human-review` with its ID
and reason. Changing the label to queued, losing local state, or waiting for the
quota window to expire does not authorize a replacement execution. Stop the
dispatcher and verify the old execution in the provider before intervening:
restore a verified handle with phase `launched` to resume it, or, only after
confirming no execution is running, archive/remove that journal block to permit
a new attempt. Never change `unknown` back to `prepared` by assumption.
This also applies when a reclaimed cloud task is intentionally retried.

The journal uses the existing single-dispatcher lock and Issue body API, not a
cross-runner compare-and-swap. Do not run independent dispatchers against the
same task concurrently. Pre-existing executions without journals retain the
legacy PR-based recovery behavior; missing historical handles cannot be inferred.

This document describes how to use the Orchestune CLI commands (`orchestune dag`, `orchestune provision`, `orchestune dispatch`) and the specification for the task decomposition plan (`decomposition_plan.md`).

---

## 1. Task Decomposition Plan Specification

To split a main development task (a "big rock") into parallelizable subtasks, create a unique Git-ignored path such as `.orchestune/tmp/decomposition-my-task-20260922T120000Z-550e8400/decomposition-plan.md`. The session directory format is `<artifact>-<issue-or-task>-<UTC timestamp>-<random>`; use a UUID or equivalent random value and pass the resulting path explicitly with `--plan`. The CLI's legacy `decomposition_plan.md` default remains available for backward compatibility, but agents must not create fixed-name drafts in the repository root or OS-global `/tmp`.
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
      - "uv run pytest tests/test_connection.py"
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
    * `dependency-manifest`: `pyproject.toml` / `package.json` / `poetry.lock` / `uv.lock` / `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml` / `Cargo.toml` / `go.mod`

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
| `dag_ignore_patterns` (or `dag-ignore-patterns`) | `[]` | List of regex strings matched only against `footprint` paths; `symbols` always remain in the similarity-scoring input. A matching path is excluded, in addition to the built-in ignore list (`pyproject.toml`, `poetry.lock`, `uv.lock`, `logging.py`, `logger.py`, `config.py`, `settings.py`), from similarity Conflict Edge scoring and heuristic shared-contract-hotspot conflicts. A pair can still conflict through another unignored path or a shared `symbols` entry. Explicit `shared_contract` writer conflicts and the independent writer warning are unaffected. The Precedence DAG contains only explicit `depends_on` edges, so this setting cannot affect `DagCycleError`. Empty strings are rejected because they match every path. |
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

Routine dispatch execution uses strictly the following 6 options. Detailed parameters (rate limits, token budgets, timeouts, paths, etc.) are configured via configuration files (`orchestune.toml`) or environment variables.

| Option | Default | Description |
| :--- | :--- | :--- |
| `--parent-issue <int>` / `-p <int>` | - | The parent GitHub Issue number coordinating this plan. If omitted, inferred from the current Git branch name (`parent/issue-<N>`). If neither is available, startup fails with an error. Created sub-issues will link to this parent. |
| `--apply` / `--no-apply` | `--apply` | Choose whether to actually execute actions (worktree setup, API calls) or preview them (dry-run). |
| `--dispatch-target {local,cloud-routine,codex-cloud,claude-cli,agy-cli,codex-cli,auto}` | auto-selected (non-CI: `auto` / GitHub Actions: `cloud-routine`) | Target environment to launch agents. When unspecified, resolved from configuration file or auto-selected from runtime environment (`GITHUB_ACTIONS`). `auto` detects a local CLI on `PATH`. `local` gives the backward-compatible no-op dummy (for tests/dry-runs). |
| `--max-concurrent <int>` | `2` (when unset in config) | Maximum number of subtask agents running concurrently. CLI argument overrides configuration file setting. |
| `--profile <name>` | - | Override the task execution profile (e.g. `balanced`, `fast-code`, `deep-reasoning`) for this entire run, taking precedence over task metadata profile or model tier. |
| `--allow-unsafe-agent-execution` | `False` | Explicitly permits bypassing approvals and sandboxing (full-permission execution) for local CLIs (`claude-cli`, `agy-cli`, `codex-cli`). For safety, this flag is accepted only via CLI (prohibited in configuration files). Attempting to run a local CLI target without this flag fails closed with an error. |

### Configuration File (`orchestune.toml`) for Detailed Settings

Non-routine options (storage paths, rate limits, timeouts, reviewer selection, consistency loops, etc.) are centralized in the repository's configuration file (`orchestune.toml` or the `[tool.orchestune]` section of `pyproject.toml`). Copy the template with `cp orchestune.toml.example orchestune.toml`. Keep the real file as untracked local configuration and publish shared changes through `orchestune.toml.example`.

#### Configuration File Settings

| Setting Key | Default | Description |
| :--- | :--- | :--- |
| `reviewer-bot` | `"auto"` | Reviewer requested after implementation (`"auto"`, `"claude"`, `"codex"`). `auto` evaluates target type and maps Claude targets to Codex, and Codex/agy targets to Claude. |
| `ci-command` | `"./scripts/local-ci.sh"` | The CI command the Integrator runs on the integration branch (a shell-like string parsed with shlex or string list, e.g. `"make ci"`). Set this explicitly if your repository's CI entrypoint differs. |
| `max-launches-per-window` | `1` | Rate limiting: maximum number of agent launches allowed in `window-seconds`. |
| `window-seconds` | `3600` | The sliding window duration in seconds for launch rate-limiting and token quotas (default is 1 hour). |
| `deviation-buffer-lines` | `5` | Allowed line modifications buffer outside the declared footprint to prevent live-locks. |
| `max-recompute-retries` | `2` | Maximum runtime Conflict Graph recomputation retries after a footprint deviation is detected. Exceeding it falls back to forced serialization (force-serial). |
| `task-timeout-seconds` | `0` | Seconds after which a running task is treated as timed out and reclaimed by the GC. `0` (default) disables timeout reclamation and only detects zombies. |
| `max-task-reclaims` | `3` | Maximum number of times the zombie/timeout GC may return the same task to `status:queued`. Once exceeded, the task moves to `status:blocked-human-review`. |
| `early-death-window-seconds` | `120` | Treat a no-commit local process exit within this many seconds of launch as a transient startup failure. |
| `max-early-death-retries` | `2` | Maximum automatic requeues for transient startup failures. The next no-commit exit escalates to `status:blocked-human-review`. |
| `early-death-backoff-seconds` | `60` | Base delay for an early-death requeue. Each retry doubles the previous delay. |
| `zombie-gc` | `true` | Enable zombie process detection and reclamation. |
| `max-tokens-per-window` | `None` | Quota limit: maximum total tokens consumed across completed tasks within `window-seconds`. Pauses new launches when reached. |
| `max-tokens-per-task` | `None` | Per-task limit: maximum token consumption allowed for a single subtask before escalating to `status:blocked-human-review`. |
| `local-cmd` | `None` | Command template for dispatching to a local target. Available placeholders: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}`, `{model}`, `{reasoning_effort}`, `{profile}`, and `{reviewer_bot}`. |
| `routine-id` | `None` | Cloud Routine ID for `cloud-routine` target. The `ORCHESTUNE_ROUTINE_ID` environment variable takes precedence. |
| `codex-cloud-env` | `None` | Codex Cloud environment ID for `codex-cloud` target. The `ORCHESTUNE_CODEX_CLOUD_ENV` environment variable takes precedence. |
| `consistency-mode` | `"off"` | Additional repository-wide consistency loop (`"off"`, `"shadow"`, `"repair"`). |
| `consistency-repair-code` | `[]` | List of finding codes or command codes allowed in the `repair` loop. |
| `consistency-max-repair-passes` | `1` | Maximum guarded repair/re-observation passes per dispatch cycle (1-5). |
| `run-state-path` | `"run_state.json"` | Where the run state carried across dispatch cycles is persisted. Relative paths resolve against the primary checkout root. |
| `worktree-root` | `"worktrees"` | Root directory for agent worktrees. Relative paths resolve against the primary checkout root. |
| `log-dir` | `"logs"` | Directory where agent execution logs are stored. |
| `events-log-path` | `"events.jsonl"` | File path for dispatch event logging. |
| `not-needed-review-state-path` | `"not_needed_review_state.json"` | State file for pending not-needed reviews on Cloud Routine targets. |
| `not-needed-review-timeout-seconds` | `86400` | Timeout in seconds for pending not-needed reviews. |
| `default_execution_profile` | `"balanced"` | Default profile name when none is specified by task or CLI. |

The default self-healing allowlist is intentionally separate from `consistency-repair-code`. It contains `status.blocked-with-resolved-dependencies`, `status.primary-status-conflict`, `execution.requeue`, `execution.update-bookkeeping`, and `execution.reclaim`, preserving the status promotion/reconciliation, state recovery, and GC behavior that predates the optional loop. Codes that reached a built-in repair pass are not attempted again by the later repository-wide repair loop; commands that appeared only as planner candidates remain eligible for the user allowlist. Opted-in execution commands use the same guarded GC and recovery handlers as the built-in boundaries.

Use `off` for unchanged behavior, `shadow` to inspect additional start/end findings, `repair` with no repair codes to inspect final dispositions without enabling a new policy, and then a limited set of `consistency-repair-code` options to opt in. `--apply` permits the established repairs and opted-in policies to mutate; `--no-apply` permits no external or durable repair side effects (GC output is a preview and recovery may update only ephemeral in-memory preview bookkeeping).

Inspect `consistency.scans`, `consistency.repair_passes`, and `consistency.repair_outcomes` in `--json` output or `events.jsonl`. Outcomes are `resolved`, `unresolved`, `deferred`, `failed`, or `observation-unknown`. Unknown/stale observations and non-repairable findings remain visible without being mutated. A skipped command-level result caused by dry-run or a failed live precondition is represented by the finding's final disposition; there is no fallback to an old phase-owned repair path. A failed partial status transition leaves its Intent journal beside `run_state.json` so the next cycle can resume it without duplicating the external side effect.

### Cloud Environment Variables and Secrets

Authentication secrets and identifiers for cloud targets are configured through environment variables:

| Environment Variable | Purpose | Precedence & Constraints |
| :--- | :--- | :--- |
| `ORCHESTUNE_ROUTINE_TOKEN` | Claude Code Cloud Routine API authentication token | **Environment variable only** (strictly prohibited in configuration files for security) |
| `ORCHESTUNE_ROUTINE_ID` | Claude Code Cloud Routine ID | Environment variable > configuration file (`routine-id`) |
| `ORCHESTUNE_CODEX_CLOUD_ENV` | Codex Cloud environment ID | Environment variable > configuration file (`codex-cloud-env`) |

### CLI Arguments Migration Table

Non-routine options previously exposed via CLI flags have been migrated to configuration files or environment variables as follows. Passing removed CLI flags will result in an unrecognized argument error:

| Former CLI Flag | New Configuration Setting | Notes |
| :--- | :--- | :--- |
| (Former) `--parent-issue <int>` | `-p <int>` / `--parent-issue <int>` or branch inference | Retained on CLI (added `-p` shortcut; prohibited in TOML) |
| (Former) `--model <name>` | `[execution_profiles.<name>.<target>] model` | Configured per target inside profiles |
| (Former) `--reasoning-effort <effort>` | `[execution_profiles.<name>.<target>] reasoning_effort` | Configured per target inside profiles |
| (Former) `--reviewer-bot <bot>` | Config setting `reviewer-bot = "..."` | Centralized in config file |
| (Former) `--local-cmd <cmd>` | Config setting `local-cmd = "..."` | Centralized in config file |
| (Former) `--routine-id <id>` | `ORCHESTUNE_ROUTINE_ID` or config `routine-id` | Env var takes precedence |
| (Former) `--routine-token <token>` | `ORCHESTUNE_ROUTINE_TOKEN` | Env var only (prohibited in TOML) |
| (Former) `--codex-cloud-env <id>` | `ORCHESTUNE_CODEX_CLOUD_ENV` or config `codex-cloud-env` | Env var takes precedence |
| (Former) `--ci-command <cmd>` | Config setting `ci-command = "..."` | Centralized in config file |
| (Former) `--max-launches-per-window` | Config setting `max-launches-per-window` | Centralized in config file |
| (Former) `--window-seconds` | Config setting `window-seconds` | Centralized in config file |
| (Former) `--max-tokens-per-window` | Config setting `max-tokens-per-window` | Centralized in config file |
| (Former) `--max-tokens-per-task` | Config setting `max-tokens-per-task` | Centralized in config file |
| (Former) `--deviation-buffer-lines` | Config setting `deviation-buffer-lines` | Centralized in config file |
| (Former) `--max-recompute-retries` | Config setting `max-recompute-retries` | Centralized in config file |
| (Former) `--task-timeout-seconds` | Config setting `task-timeout-seconds` | Centralized in config file |
| (Former) `--max-task-reclaims` | Config setting `max-task-reclaims` | Centralized in config file |
| (Former) `--early-death-window-seconds` | Config setting `early-death-window-seconds` | Centralized in config file |
| (Former) `--max-early-death-retries` | Config setting `max-early-death-retries` | Centralized in config file |
| (Former) `--early-death-backoff-seconds` | Config setting `early-death-backoff-seconds` | Centralized in config file |
| (Former) `--zombie-gc` | Config setting `zombie-gc` | Centralized in config file |
| (Former) `--consistency-mode` | Config setting `consistency-mode` | Centralized in config file |
| (Former) `--consistency-repair-code` | Config setting `consistency-repair-code` | Centralized in config file |
| (Former) `--consistency-max-repair-passes` | Config setting `consistency-max-repair-passes` | Centralized in config file |
| (Former) `--run-state-path` | Config setting `run-state-path` | Centralized in config file |
| (Former) `--worktree-root` | Config setting `worktree-root` | Centralized in config file |
| (Former) `--log-dir` | Config setting `log-dir` | Centralized in config file |
| (Former) `--events-log-path` | Config setting `events-log-path` | Centralized in config file |
| (Former) `--not-needed-review-state-path` | Config setting `not-needed-review-state-path` | Centralized in config file |
| (Former) `--not-needed-review-timeout-seconds` | Config setting `not-needed-review-timeout-seconds` | Centralized in config file |
| (Former) `--allow-unsafe-agent-execution` | `--allow-unsafe-agent-execution` | Retained on CLI (prohibited in TOML) |

#### Example Config (`orchestune.toml`)
```toml
max-concurrent = 2
dispatch-target = "claude-cli"
reviewer-bot = "auto"
consistency-mode = "shadow"
consistency-repair-code = []
consistency-max-repair-passes = 1
run-state-path = "run_state.json"
default_execution_profile = "balanced"

[execution_profiles.balanced.claude-cli]
model = "sonnet"
reasoning_effort = "medium"

[execution_profiles.balanced.codex-cli]
model = "gpt-5.6-terra"
reasoning_effort = "medium"

[execution_profiles.deep-reasoning.claude-cli]
model = "opus"
reasoning_effort = "high"

[execution_profiles.deep-reasoning.codex-cli]
model = "gpt-5.6-sol"
reasoning_effort = "high"

[execution_profiles.deep-reasoning.cloud-routine]
model = "claude-opus-5"

[execution_profiles.fast-code.claude-cli]
model = "haiku"

[execution_profiles.fast-code.codex-cli]
model = "gpt-5.6-luna"
reasoning_effort = "medium"
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
run-state-path = "run_state.json"
default_execution_profile = "balanced"

[tool.orchestune.execution_profiles.balanced.claude-cli]
model = "sonnet"
reasoning_effort = "medium"

[tool.orchestune.execution_profiles.balanced.codex-cli]
model = "gpt-5.6-terra"
reasoning_effort = "medium"

[tool.orchestune.execution_profiles.deep-reasoning.claude-cli]
model = "opus"
reasoning_effort = "high"

[tool.orchestune.execution_profiles.deep-reasoning.codex-cli]
model = "gpt-5.6-sol"
reasoning_effort = "high"
```

> [!NOTE]
> Setting keys can be written in either kebab-case (e.g., `max-concurrent`) to match CLI options, or snake_case (e.g., `max_concurrent`) to match internal variables.
> If an option is explicitly specified as a command-line argument, it overrides the value in the configuration file.
> Unknown keys and invalid values stop startup with an error rather than falling back to defaults. Parent issue (`parent_issue`), unsafe execution bypass (`allow_unsafe_agent_execution`), routine tokens (`routine_token`), and top-level `model`/`reasoning_effort` are prohibited in configuration files. Boolean settings must be TOML booleans, paths and string settings must be strings, and integer settings must be TOML integers. `consistency-repair-code` must be a list of non-empty strings. `max-concurrent`, `max-launches-per-window`, `deviation-buffer-lines`, `max-recompute-retries`, `task-timeout-seconds`, `max-task-reclaims`, `early-death-window-seconds`, `max-early-death-retries`, `early-death-backoff-seconds`, and `not-needed-review-timeout-seconds` must be at least `0`; `window-seconds` must be at least `1`, and `consistency-max-repair-passes` must be between `1` and `5`.
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

The required `--parent-issue` selects the parent branch and temporary integration branch:

| `--parent-issue` | Base branch | Temporary integration branch |
| :--- | :--- | :--- |
| `N` | `origin/parent/issue-{N}` | `integration/temp-parent-issue-{N}` |

### 4.2 Two-tier integration via a parent branch

Integration is two-tiered: "child branches → parent branch" and "parent branch → main".

1. **Child branches → parent branch (automatic)**: Child PRs are automatically integrated into the `parent/issue-{N}` branch. Once the integration PR passes CI it is auto-merged without waiting for human approval, and the corresponding child issues are closed automatically. The individual child PRs opened by agents therefore do not need to be merged by a human; they remain as a review record.
   - If the auto-merge fails (branch protection, permissions, and so on), a comment is posted on the affected issues and the merge is retried automatically on the next dispatch cycle.
2. **Parent branch → main (merged by a human)**: Once every child issue under the parent is closed, a final integration PR from `parent/issue-{N}` to `main` is prepared automatically. **Deciding whether to merge that final PR, and performing the merge, is always done by a human.** When the final PR's merge is detected, the parent issue is closed automatically.

### 4.3 Auto-Rebase

Downstream dependent task branches are rebased automatically depending on the state of the tasks they depend on. The rebase target is **the branch of the dependency whose PR has already passed CI** (stacking), not "the latest main". No auto-rebase happens when the dependency cannot be narrowed down to a single branch, or when the dependency has not passed CI yet.

### 4.4 Issue ↔ PR link notices

GitHub's `Closes #N` auto-linking and the "Development" sidebar on an issue only work when the PR targets the default branch (`main`). Under the parent-branch workflow that leaves a child issue with no visible trace of the PR that implemented it, so Orchestune fills the gap with comments:

1. **When a PR is opened**: once the dispatcher sees an open PR, it posts a "PR #XXX has been opened" notice on the corresponding child issue. The target issue is resolved both from the PR's `Closes #N` references and from the head branch name (`claude/issue-{N}-{subtask_id}`), so PRs opened by the agent itself are announced through the same path. A notice is posted **only when the PR's base is exactly that issue's own parent branch** (`parent/issue-{parent_issue_number}`), so a PR targeting a different parent branch that merely references the issue is ignored. The PR's head must also live in the upstream repository: PRs from forks, and any PR whose head origin cannot be confirmed, are skipped so that a third party cannot post an authoritative-looking notice on someone else's issue.
2. **When a PR is merged**: when the Integrator merges the integration PR into the parent branch and closes the child issue, it posts a "PR #XXX has been merged into the parent branch" completion notice *just before* closing. The notice is deliberately not folded into the closing comment: if only the close fails, the retry on the next cycle can no longer recover the integration PR number, and the link would be lost for good.

Both comments embed a `<!-- orchestune:pr-link:{created|merged}:{pr_number} -->` marker, so the same notice is never posted twice. The two notices differ in how they handle a failure to read the existing comments: the creation notice is skipped and retried on the next cycle, while the merge notice is posted anyway, because it is the last write before the issue is closed and would otherwise never be retried.

---

## 6. Replacing an unstarted decomposition generation (`orchestune replan`)

`orchestune provision` is for the initial creation of a plan's Issues. When a
parent Issue's requirements remain valid but its unstarted decomposition is
stale, use `orchestune replan` to replace that generation while retaining the
old Issues as history. Preview is always read-only:

```bash
orchestune replan --plan decomposition_plan.md --parent-issue 123
```

The preview lists `create`/`reuse` targets for the new generation and
`retire`, `manual-review`, `conflict`, or `no-op` targets for the old one, and
prints a snapshot-bound token. Apply only the exact fresh token:

```bash
orchestune replan --plan decomposition_plan.md --parent-issue 123 \
  --apply --confirm-preview replan-preview-v1:sha256:<token>
```

An in-progress, done, closed, conflicting, or merged-result Issue is never
automatically replaced. Apply verifies before its first write that the parent
Issue body carrying the new plan stays within GitHub's 65,536 character body
limit, and stops with exit code `3` without creating Issues, changing
relationships, or retiring the old generation when it would not. A partial
failure requires a new preview and token;
re-running a completed replacement makes no GitHub mutations. Exit codes are
`0` (safe preview/success), `2` (configuration), `3` (missing or invalid
approval), `4` (partial application), `5` (already-active no-op), and `6`
(preview contains conflicts or manual-review targets).

---

## 7. Claiming a Task and Preparing a Worktree (`orchestune claim`)

To begin work on a provisioned subtask issue, use the `orchestune claim` command. It safely validates prerequisites (such as dependency completion), fetches the base branch, prepares an isolated task worktree, records the active state in the local ledger, and updates the task issue's status labels in a single atomic flow.

```bash
# Claim a task by specifying its issue number
orchestune claim 123
```

Upon success, the command prints the issue number, claim ID, branch name, prepared worktree path, and base reference, and exits with code `0`. Navigate to the prepared worktree path (`cd <worktree_path>`) and start development.

### Major Options

| Option | Default | Description |
| :--- | :--- | :--- |
| `--no-apply` | disabled | Dry-run mode: validates prerequisites and previews planned values without modifying Git, GitHub, or local state. |
| `--resume <claim_id>` | none | Resumes an interrupted claim using protected local credentials. |
| `--state <path>` | `run_state.json` | Path to the run-state ledger file. |
| `--timeout <seconds>` | none | Timeout in seconds for acquiring the run-state lock. |

### Failure Handling

If a claim cannot proceed due to unmet dependencies, conflicts, or environmental errors, the command exits with a non-zero exit code and outputs the failure reason along with recommended next actions to stderr. Follow the diagnostic instructions to resolve conflicts or resume an interrupted claim using `--resume`. If already working inside the claimed task worktree, running claim again is not needed.

## 8. Local CI Evidence Storage and Task Completion (`orchestune complete`)

Upon completing task implementation, the `orchestune complete` command verifies local CI evidence and records the Outcome Record on the task Issue.

### Evidence Storage Location and Git State
- **Default Storage Location**: `.orchestune/ci/ci_evidence.json` inside each worktree.
- **Git Ignore**: `.orchestune/ci/` is registered in `.gitignore`, ensuring evidence and atomic temporary files (`.tmp.*`) never dirty the worktree's clean git status.
- **Environment Override**: Setting `ORCHESTUNE_CI_EVIDENCE_PATH` allows specifying a custom evidence file location.
- **Permission Isolation & Sandbox Support**: Even in restricted sandbox environments or linked worktrees where the Git metadata directory (`.git` or `.git/worktrees/<name>`) is read-only, evidence invalidation, recording, and verification succeed as long as the worktree itself is writable.
- **Migration Note**: Legacy evidence previously recorded under `.git` is not reused by the new default path. After migrating, rerun local CI (`./scripts/local-ci.sh` or `.\scripts\local-ci.ps1`) to record fresh evidence.

## 9. Garbage Collecting Handoff-Ready Tasks (`orchestune gc`)

After `orchestune complete` records an Outcome and the related PR is merged, run the dedicated GC command from the primary checkout to release the reservation. It also supports interactive tasks that have no parent Issue.

```bash
orchestune gc --no-apply  # inspect decisions without changes
orchestune gc             # apply the decisions
```

The command inspects handoff-ready interactive entries (`owner_kind=interactive`) only. The dispatch cycle owns dispatch worktrees. A `done` entry is released only after it verifies the exact journaled Outcome comment, the merged PR, the PR head and base, and worktree ownership. A matching `blocked` or `not-needed` Outcome releases the reservation; a clean worktree is removed, while a dirty worktree is retained without done history or a receipt. Missing or mismatched Outcome, PR, or ownership evidence leaves the entry on hold with a reason.

`--no-apply` is a read-only preview. Apply mode operates on the local ledger and worktrees; it does not change GitHub Issues, labels, PRs, comments, branches, or start a dispatch cycle. If the current working directory is inside a worktree that would be removed, the command holds it as `current_worktree`; rerun from the primary checkout or another directory.

| Option | Default | Description |
| :--- | :--- | :--- |
| `--no-apply` | disabled | Show decisions without applying them. |
| `--state <path>` | primary checkout's `run_state.json` | Select the ledger path. Relative paths are based on the primary checkout. |
| `--timeout <seconds>` | `0` | Lock wait time for the ledger and claim lock. Negative and non-finite values are argument errors. |
