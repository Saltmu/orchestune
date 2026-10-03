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

### Progress and result files

stdout displays flushed progress with a run ID, parent, mode and phase, including on pipes. Final JSON is saved atomically; read the absolute `report saved` path. `report target` is only the intended path. Migrate `dispatch | jq` and JSON redirects to file reads. The JSON schema and GitHub Step Summary stay unchanged.

TOML `report-dir` defaults to `.orchestune/reports/dispatch`, resolved from the primary checkout even in linked worktrees. Each run saves `parent-<N>/<UTC YYYYMMDDTHHMMSSZ>-<UUID>/result.json`. `ORCHESTUNE_DISPATCH_REPORT_PATH` overrides it with an unused file path, relative to invocation cwd or absolute. Existing results, symlinks, empty values, directories and business-state/lock paths are rejected. Reruns need fresh paths; never select the newest file or reuse an earlier result.

`--no-apply` skips dispatch actions while saving local results and creating report directories/locks. `planned` means dry-run selection; `launched` means the launch procedure completed, not task completion (`local` is a dummy target). Progress failure does not stop result saving.

The dedicated `<result-name>.report.lock` remains beside the result, including for explicit paths. Exclusion covers cooperating dispatch runs; unrelated external writers are outside this guarantee. Do not remove a lock while other executions may use it.

Preserve the exit code before reading the current run result. These examples handle nonzero exits under Bash `set -e` and PowerShell native error promotion:

```bash
session_dir=$(./scripts/create-session-dir.sh dispatch-result 100)
result_path="$session_dir/dispatch-result.json"
dispatch_code=0
ORCHESTUNE_DISPATCH_REPORT_PATH="$result_path" orchestune-dispatch -p 100 || dispatch_code=$?
if [ -f "$result_path" ]; then jq . "$result_path"; else echo "report not created" >&2; fi
# dispatch_code remains available under set -e; file existence alone is not success.
```

```powershell
$sessionDir = .\scripts\create-session-dir.ps1 dispatch-result 100
$resultPath = Join-Path $sessionDir 'dispatch-result.json'
$env:ORCHESTUNE_DISPATCH_REPORT_PATH = $resultPath
$savedPreference = $PSNativeCommandUseErrorActionPreference
try {
    $PSNativeCommandUseErrorActionPreference = $false
    orchestune-dispatch -p 100
    $dispatchCode = $LASTEXITCODE
} finally {
    $PSNativeCommandUseErrorActionPreference = $savedPreference
    Remove-Item Env:ORCHESTUNE_DISPATCH_REPORT_PATH
}
if (Test-Path -LiteralPath $resultPath -PathType Leaf) { Get-Content -Raw $resultPath | ConvertFrom-Json }
else { Write-Warning 'report not created' }
```

A nonzero run can still save a complete failure report. Argument/configuration errors, output reservation failure and exceptions before a complete cycle report may leave no file (`report not created`). Check exit code and JSON: 0 success, 1 fatal/save failure, 2 retryable failure (or argument/configuration error). File existence alone is not success. There is no automatic retention; `.orchestune/reports/` is ignored here.

### Major Options

Routine dispatch execution uses strictly the following 7 options. Detailed parameters (rate limits, token budgets, timeouts, paths, etc.) are configured via configuration files (`orchestune.toml`) or environment variables.

| Option | Default | Description |
| :--- | :--- | :--- |
| `--parent-issue <int>` / `-p <int>` | - | The parent GitHub Issue number coordinating this plan. If omitted, inferred from the current Git branch name (`parent/issue-<N>`). If neither is available, startup fails with an error. Created sub-issues will link to this parent. |
| `--apply` / `--no-apply` | `--apply` | Choose whether to actually execute actions (worktree setup, API calls) or preview them (dry-run). |
| `--dispatch-target {local,cloud-routine,codex-cloud,claude-cli,agy-cli,codex-cli,auto}` | auto-selected (non-CI: `auto` / GitHub Actions: `cloud-routine`) | Target environment to launch agents. When unspecified, resolved from configuration file or auto-selected from runtime environment (`GITHUB_ACTIONS`). `auto` detects a local CLI on `PATH`. `local` gives the backward-compatible no-op dummy (for tests/dry-runs). |
| `--max-concurrent <int>` | `2` (when unset in config) | Maximum number of subtask agents running concurrently. CLI argument overrides configuration file setting. |
| `--profile <name>` | - | Override the task execution profile (e.g. `balanced`, `fast-code`, `deep-reasoning`) for this entire run, taking precedence over task metadata profile or model tier. |
| `--child-review-gate {required,off}` | - | Child sub-issue review gate mode (`required` or `off`). When omitted, falls back to config file (`child-review-gate`), env var (`ORCHESTUNE_CHILD_REVIEW_GATE`), or default `required`. `off` skips verification with a warning. See [§4.5](#45-child-review-evidence-gate). |
| `--allow-unsafe-agent-execution` | `False` | Explicitly permits bypassing approvals and sandboxing (full-permission execution) for local CLIs (`claude-cli`, `agy-cli`, `codex-cli`). For safety, this flag is accepted only via CLI (prohibited in configuration files). Attempting to run a local CLI target without this flag fails closed with an error. |

### Configuration File (`orchestune.toml`) for Detailed Settings

Non-routine options (storage paths, rate limits, timeouts, reviewer selection, consistency loops, etc.) are centralized in the repository's configuration file (`orchestune.toml` or the `[tool.orchestune]` section of `pyproject.toml`). Copy the template with `cp orchestune.toml.example orchestune.toml`. Keep the real file as untracked local configuration and publish shared changes through `orchestune.toml.example`.

#### Configuration File Settings

| Setting Key | Default | Description |
| :--- | :--- | :--- |
| `reviewer-bot` | `"auto"` | Reviewer requested after implementation (`"auto"`, `"claude"`, `"codex"`). `auto` evaluates target type and maps Claude targets to Codex, and Codex/agy targets to Claude. |
| `ci-command` | `"./scripts/local-ci.sh"` | The CI command the Integrator runs on the integration branch (a shell-like string parsed with shlex or string list, e.g. `"make ci"`). Set this explicitly if your repository's CI entrypoint differs. |
| `child-review-gate` | `"required"` | Child sub-issue review gate mode (`"required"`, `"off"`). Verifies review evidence (`verdict=pass`, SHA match) before merging to parent branch. `"off"` skips verification with a warning. |
| `max-launches-per-window` | unset (no limit) | Time-based launch cap within `window-seconds`. Unset: no cap (concurrency via `max-concurrent` is the primary control; token budget, conflicts, etc. still apply). `0`: launches are prohibited, while label updates, GC and other processing still run. `1` or more: maximum launches per window. Omit the key to leave it unset (TOML has no null). |
| `window-seconds` | `7200` | Sliding window in seconds (default 2 hours) for the launch cap, `max-tokens-per-window` aggregation, aging normalization, and launch-history retention. |
| `deviation-buffer-lines` | `5` | Allowed line modifications buffer outside the declared footprint to prevent live-locks. |
| `max-recompute-retries` | `2` | Maximum runtime Conflict Graph recomputation retries after a footprint deviation is detected. Exceeding it falls back to forced serialization (force-serial). |
| `task-timeout-seconds` | `7200` | Seconds after which a dispatch-launched task is treated as timed out. `0` disables timeout reclamation. Interactive claims are never reclaimed by the timeout (the GC zombie reclaim excludes them). Local executions are reclaimed after a WIP backup; an external (cloud) execution whose stop cannot be confirmed keeps its `active_worktrees` slot and handle and is sent to `status:blocked-human-review` instead. Normal tasks longer than 2 hours are reclaimed and requeued (up to `max-task-reclaims`, then `status:blocked-human-review`), so extend this value in `orchestune.toml` for long-running work. |
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
| `report-dir` | `.orchestune/reports/dispatch` | Automatic result root, relative to primary checkout; overridden per run by `ORCHESTUNE_DISPATCH_REPORT_PATH`. |
| `run-state-path` | `"run_state.json"` | Where the run state carried across dispatch cycles is persisted. Relative paths resolve against the primary checkout root. |
| `worktree-root` | `"worktrees"` | Root directory for agent worktrees. Relative paths resolve against the primary checkout root. |
| `log-dir` | `"logs"` | Directory where agent execution logs are stored. |
| `events-log-path` | `"events.jsonl"` | File path for dispatch event logging. |
| `not-needed-review-state-path` | `"not_needed_review_state.json"` | State file for pending not-needed reviews on Cloud Routine targets. |
| `not-needed-review-timeout-seconds` | `86400` | Timeout in seconds for pending not-needed reviews. |
| `integration-dependency-timeout-seconds` | `600` | Upper bound in seconds for one Integrator dependency preparation (`uv sync`). Positive integers only. |
| `integration-ci-timeout-seconds` | `1800` | Upper bound in seconds for one Integrator CI command. The effective wait is `min(this, remaining cycle time)`. |
| `integration-cycle-timeout-seconds` | `3600` | Execution budget in seconds for one parent Issue's integration cycle (lock wait, preparation, merge, dependencies, CI, PR handling, finalization). Created once per cycle on a monotonic clock. |
| `integration-cleanup-timeout-seconds` | `30` | One independent budget in seconds for stopping processes, draining output, rolling back and recording after the deadline. It is not renewed per step. |
| `integration-command-timeout-seconds` | `60` | Upper bound in seconds for one auxiliary `git`/`gh` command inside the cycle. |
| `max-integration-timeout-retries` | `2` | Automatic retries after a **confirmed** integration timeout (3 attempts by default). `0` makes the first timeout terminal. |
| `integration-timeout-backoff-seconds` | `60` | Wait before the first retry; the second waits twice as long (60 s, then 120 s by default). |
| `default_execution_profile` | `"balanced"` | Default profile name when none is specified by task or CLI. |

The default self-healing allowlist is intentionally separate from `consistency-repair-code`. It contains `status.blocked-with-resolved-dependencies`, `status.primary-status-conflict`, `execution.requeue`, `execution.update-bookkeeping`, and `execution.reclaim`, preserving the status promotion/reconciliation, state recovery, and GC behavior that predates the optional loop. Codes that reached a built-in repair pass are not attempted again by the later repository-wide repair loop; commands that appeared only as planner candidates remain eligible for the user allowlist. Opted-in execution commands use the same guarded GC and recovery handlers as the built-in boundaries.

Use `off` for unchanged behavior, `shadow` to inspect additional start/end findings, `repair` with no repair codes to inspect final dispositions without enabling a new policy, and then a limited set of `consistency-repair-code` options to opt in. `--apply` permits the established repairs and opted-in policies to mutate; `--no-apply` permits no external or durable repair side effects (GC output is a preview and recovery may update only ephemeral in-memory preview bookkeeping).

Inspect `consistency.scans`, `consistency.repair_passes`, and `consistency.repair_outcomes` in the saved dispatch JSON or `events.jsonl`. Outcomes are `resolved`, `unresolved`, `deferred`, `failed`, or `observation-unknown`. Unknown/stale observations and non-repairable findings remain visible without being mutated. A skipped command-level result caused by dry-run or a failed live precondition is represented by the finding's final disposition; there is no fallback to an old phase-owned repair path. A failed partial status transition leaves its Intent journal beside `run_state.json` so the next cycle can resume it without duplicating the external side effect.

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
> Unknown keys and invalid values stop startup with an error rather than falling back to defaults. Parent issue (`parent_issue`), unsafe execution bypass (`allow_unsafe_agent_execution`), routine tokens (`routine_token`), and top-level `model`/`reasoning_effort` are prohibited in configuration files. Boolean settings must be TOML booleans, paths and string settings must be strings, and integer settings must be TOML integers. `consistency-repair-code` must be a list of non-empty strings. `max-concurrent`, `max-launches-per-window`, `deviation-buffer-lines`, `max-recompute-retries`, `task-timeout-seconds`, `max-task-reclaims`, `early-death-window-seconds`, `max-early-death-retries`, `early-death-backoff-seconds`, `not-needed-review-timeout-seconds`, and `max-integration-timeout-retries` must be at least `0`; `window-seconds` and the five `integration-*-seconds` settings (plus `integration-timeout-backoff-seconds`) must be at least `1`, and `consistency-max-repair-passes` must be between `1` and `5`.
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

### 4.5 Child review-evidence gate

Before the integrator updates `parent/issue-{N}` (the auto-merge in 4.2), it verifies that every child in the merge has passing review evidence. This is the required layer-1 gate. The Semantic Review of the integration PR (layer 2) stays advisory and never blocks the merge ([architecture/integration.md](architecture/integration.md)).

**Who does what**

| Actor | Responsibility |
| :--- | :--- |
| Development skill (review loop, Step 11) | After the PR is created, requires an explicit reviewer selection (`claude` / `codex` / `skip`), runs the review on the child PR, and records an LLM judgment (`adopt` / `decline` / `already_addressed` / `needs_information` / `duplicate`) for every finding in a judgment table. |
| `orchestune complete --issue <N> --pr <PR> --result done --reviewer <bot> --review-reply <file>` | Re-acquires the PR's review state and checks that the table covers every current finding, that none is `unresolved`, `needs_information` or a required `deferred`, and that the review target SHA equals the PR head and the local HEAD. It saves `verdict`, `reviewed_head_sha` and the judgment digest in the done Outcome Record. On any mismatch it rejects the completion (`review_evidence_invalid`, `review_head_mismatch` or `evidence_missing`) and posts nothing. `skip` is recorded as `verdict=skipped`, never as a pass. |
| Integrator | Only verifies the saved evidence; it never runs a review. |

**Pass condition (per child)**: the child Issue's latest Outcome Record has `result=done` and `verdict=pass`, and both its `head_sha` and `reviewed_head_sha` equal the commit SHA about to be merged. A rebase or push after the review changes that SHA, so it must be reviewed again.

**When the gate stops the integration**: the parent branch is not updated, no child Issue is closed, no child branch is deleted, and the **parent Issue** moves to `status:blocked-human-review` with one comment (marker `<!-- orchestune:child-review-gate digest=… -->`) listing each child and its reason. The same failure set is not commented again on later cycles; the label is only restored if it is missing. The gate is re-evaluated on every integration cycle, so once the evidence is fixed the same integration proceeds. The gate does not remove the parent's `status:blocked-human-review` itself (see [status-labels.md](status-labels.md)).

| Reason | Meaning | How to resume |
| :--- | :--- | :--- |
| `legacy` | The Outcome has no review evidence (it was recorded before the gate existed) | See “Resuming” below |
| `skipped` | The review was explicitly skipped | See “Resuming” below |
| `not_pass` | `verdict` is not `pass`, or `result` is not `done` | Resolve the findings so the review passes, then see “Resuming” |
| `sha_mismatch` | The child head moved after `complete` (rebase or push) | Review the new head, then see “Resuming” |
| `absent` | The child Issue has no Outcome Record | Run `orchestune complete` for the child |
| `lookup_unknown` | Reading the child Issue's comments failed (e.g. API error) | Re-run dispatch; no action on the child |
| `integration_evidence_missing` | The integration proof or the child mapping is missing | Restore the integration evidence and re-run |

**Resuming**

- **Before the completion is handed off** (`complete` was rejected, so nothing was posted): fix the cause (review the current head again, complete the judgment table), then re-run `orchestune complete`. The evidence is then recorded and the next cycle integrates the child.
- **After a completion was already handed off with insufficient evidence** (`legacy`, `skipped`, `not_pass`, `sha_mismatch`): re-running `complete` in the same claim cannot add or replace evidence. The same request only replays the stored result, and a request with different review arguments is rejected with `request_fingerprint_mismatch`. Resume by explicitly turning the gate off (below) for the run that must proceed. This verifies nothing for **every** child in that run, so use it only for children you accept without review evidence.
- **Clearing the parent label (both paths)**: the gate never removes the parent Issue's `status:blocked-human-review`, so remove it yourself once the evidence is corrected or accepted. Doing so before the re-run is safe: if the gate stops the integration again, it restores the label without posting the same comment twice. Otherwise remove it after the integration succeeds, before the parent Issue is closed.

**Settings**: `--child-review-gate {required,off}`, the `child-review-gate` configuration key and `ORCHESTUNE_CHILD_REVIEW_GATE`; the default is `required`. `off` skips the verification for all children in the run and prints a warning. It is an explicit opt-out, never selected automatically.

**Migration**: Outcome Records posted before this gate have no review evidence and stop as `legacy`. During the migration period either (a) set `off` explicitly until the in-flight children are integrated, or (b) accept the stop and, for children that have not yet been handed off, review on the child PR and run `orchestune complete` again; for children already handed off, use (a). Return to `required` once the legacy children are integrated.

### 4.6 Bounded integration execution and timeout recovery

Dependency preparation (`uv sync`), the CI command and the whole integration cycle of one parent Issue are bounded (#820). The seven `integration-*` / `max-integration-timeout-retries` settings are listed in [the configuration table](#configuration-file-orchestunetoml-for-detailed-settings); there is no CLI flag or environment variable for them, and `0` never means "unlimited".

**One cycle, in order**: take the parent execution lock (`worktrees/.locks/integration-parent-issue-<N>-execution.lock`, held until the result is recorded) → merge a child into the temporary branch → write a `reserved` event on the parent Issue and read it back → run dependency preparation and CI once each → on a timeout stop the whole process tree, roll back to the pre-merge SHA, confirm `HEAD`, and record a `finished` event. The cycle deadline is one monotonic reading taken when the parent's cycle starts; every stage waits for `min(stage limit, remaining cycle time)`, and nothing new starts after the deadline. Stopping, draining output, rolling back and recording share the single `integration-cleanup-timeout-seconds` budget.

| Status | Meaning | What happens next |
| :--- | :--- | :--- |
| `execution_timed_out` | A stage timed out (or the deadline passed); the stop and the rollback are **confirmed**. | Nothing is pushed, included, closed or deleted, and the child Issues stay `status:done` (they are **not** re-queued). The next cycle after the back-off (`integration-timeout-backoff-seconds`, then twice that) retries automatically, up to `max-integration-timeout-retries`. |
| `execution_retry_exhausted` | The last allowed attempt timed out (the third by default). | A `terminal` event is saved and the **parent Issue** goes to `status:blocked-human-review`. No further CI starts, even if labelling failed; only the label and notice are retried. |
| `execution_cleanup_failed` | A stop, the rollback, the `HEAD` check or the cleanup budget could not be confirmed. | The worktree and diagnostics are **held** (never reused, deleted or collected), no new CI starts, and the parent goes to human review. |
| `execution_indeterminate` | A push/PR write timed out with an unknown result, the history could not be read or is inconsistent, a reservation or result could not be confirmed, or an earlier attempt has no recorded result. | Nothing is guessed, retried or rolled back remotely; a write whose result is unknown is held for reconciliation. |

An ordinary non-zero CI exit or start failure is **not** a timeout: it keeps the existing behaviour (the child is re-queued with the CI output) and neither adds to nor clears the timeout count. A confirmed normal success closes the current generation and resets the count. The count is bound to the parent Issue and its generation, not to the run id, the task set, the CI command or the settings, so a new runner or a lowered limit cannot erase it. The final report and the Dispatcher result show each failure's cause, target, stage, configured and effective limits, attempt, next retry time, stop/rollback/write confirmations and the output tail; the Dispatcher retries only `execution_timed_out` and reports the other three as human-review warnings.

**Releasing a hold or a terminal state (operator procedure)**

1. Confirm that no CI process of that run is still running, and inspect the held worktree (`worktrees/integration-temp-*`) and the remote refs (`parent/issue-<N>`, `integration/temp-*`). A write whose result was unknown must be reconciled against the real refs and the existing integration evidence first.
2. Post one **reset** comment on the **parent Issue** (as the executing identity or a user with `write`/`maintain`/`admin`). `generation` is the current generation plus one; `references` names the terminal or unconfirmed `attempt_id` found in the earlier event comments, or `local-hold:<hold file name without .json>` for a hold that has no GitHub attempt behind it:

<!-- orchestune:integration-execution:v1 -->
```json
{
  "parent_issue_number": 123,
  "generation": 2,
  "attempt_id": "reset-2026-01-01-ops",
  "event": "reset",
  "targets": [],
  "executed_at": "2026-01-01T00:00:00Z",
  "reason": "Processes, worktree and remote refs verified by <operator>",
  "references": "<attempt_id of the terminal or unconfirmed attempt, or local-hold:<hold file name without .json>>"
}
```

3. Remove the worktree and its hold record (`worktrees/.holds/<key>.json`) yourself. A hold blocks new CI for its parent until a reset opens a newer generation, and it is never collected automatically.

Removing `status:blocked-human-review` alone does **not** reset anything, because the count lives in the event comments, not in a label.

**Guarantees and limits.** Waiting is bounded and a confirmed stop is required before a timeout is retried: on Linux/macOS the command runs in its own session/process group (`SIGTERM`, at most 5 s of grace, then `SIGKILL`); on Windows it is created suspended, assigned to a kill-on-close Job Object and then resumed, and if the Job assignment fails the command is not run. Output is read concurrently into bounded tails and draining uses the same cleanup budget. Auxiliary `git`/`gh` calls are bounded by their direct child's timeout only; a hook, SSH or credential-helper descendant of a scoped `git` command is not owned or stopped. Not covered: the operating system's process-creation API and uninterruptible kernel I/O (a strict wall-clock limit is not promised), a POSIX daemon that deliberately leaves the process group, concurrent applies against one parent from different hosts (GitHub comments have no atomic compare-and-swap, so a detected conflict stops), and the worker's `task-timeout-seconds`, which is a separate budget and does not bound the Integrator. The first version of this support is verified on Linux and Windows by the CI matrix; macOS shares the POSIX implementation.

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
| `--resume <claim_id>` | none | Resumes an interrupted claim using its expected generation and verified local worktree. |
| `--amend-footprint` | disabled | Widens the held claim's file reservation. Cannot be combined with `--resume`. See below. |
| `--state <path>` | `run_state.json` | Path to the run-state ledger file. |
| `--timeout <seconds>` | none | Timeout in seconds for acquiring the run-state lock. |

### Failure Handling

If a claim cannot proceed due to unmet dependencies, conflicts, or environmental errors, the command exits with a non-zero exit code and outputs the failure reason along with recommended next actions to stderr. Follow the diagnostic instructions to resolve conflicts or resume an interrupted claim using `--resume`. If already working inside the claimed task worktree, running claim again is not needed.

When re-running `orchestune claim <N>` for an issue you already hold, it fails with `existing_claim_unrecovered` and prints the worktree path, the `--resume` command, and the `--amend-footprint` command as next actions.

### Footprint overlap warnings

An interactive claim succeeds when its only conflict is a `footprint` overlap with another **interactive** reservation. The command then prints a `Warning:` to stderr with the other Issue number, branch, worktree, and the overlapping files (also in the `--no-apply` preview). When you see it, agree on the merge order with the other task; whichever merges later resolves the conflicts when rebasing.

An interactive claim is still rejected with `claim_conflict` when:

- the overlapping reservation belongs to dispatch (an autonomous agent launched by the Dispatcher)
- either side is a repository reservation (`repository_reservation`)
- the overlapping reservation is `forced_serial`
- both write the same `shared_contract` (`shared_contract`)

A dispatch claim is rejected on any footprint overlap, whoever holds the other reservation.

### Widening the reservation (`--amend-footprint`)

If the task turns out to need files outside its held file reservation, add them to the `footprint` in the Issue body and run:

```bash
orchestune claim <N> --amend-footprint --no-apply  # preview
orchestune claim <N> --amend-footprint
```

The new footprint is the union of the held footprint, the Issue footprint, and every file already changed in the worktree since the claim base (committed, uncommitted, and untracked). It never shrinks. The command re-checks conflicts against every other active reservation and, on conflict, changes nothing and reports the conflicting issue. A footprint overlap with an interactive reservation is reported as a warning, as for a new claim, and the reservation is still widened. On success it adds missing files to the Issue footprint and updates the ledger; the worktree, branch, claim ID, and labels stay unchanged. Only completed interactive file reservations in the claiming workspace (with its matching claim marker) are eligible. Switching to a repository reservation is not supported.

## 8. Local CI Evidence Storage and Task Completion (`orchestune complete`)

`orchestune complete` succeeds when it has posted the fixed Outcome Record, confirmed the corresponding Issue label (`status:done`, `status:blocked`, or `status:not-needed`), and durably saved the handoff and replay receipt in the shared ledger. Success does not mean the PR is merged, an independent not-needed review is approved, or the worktree is collected. `done` validates local CI, PR/head, child review evidence ([§4.5](#45-child-review-evidence-gate)) and token-limit evidence before publication; GC consumes that evidence. Legacy completion paths still perform their own token-limit checks.

Run `orchestune complete --issue <N> --pr <PR> --result done` from the claimed worktree; `blocked` requires `--reason`, and `not-needed` can also reserve an unclaimed Issue without creating a worktree. The command prints a completion ID before remote effects. To resume interrupted publication, repeat the same arguments with `--completion-id <ID>` using the matching claim marker, or the explicit completion ID for an unclaimed Issue. A changed request or owner/generation cannot overwrite the frozen request. After handoff, replay returns the stored result even if a later policy queued the Issue or GC removed the active entry; it does not restore old labels.

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

Both the Dispatcher and `orchestune gc` discover verified completion journals and pending downstream policies independently of the Issue's `status:in-progress` label. Only matching label-confirmed handoff and replay receipt evidence allows new-format processing. Pending publication, inconsistent or unavailable evidence, and unverified legacy handoffs are retained. Legacy tasks without completion reservations keep their existing PR/cloud/Outcome detection paths.

The standalone command physically collects interactive worktrees; the Dispatcher collects dispatch worktrees through the same guarded collector. `done` requires the exact Outcome comment, a merged PR with matching head/base, reachable merge commit, worktree head and ownership. Clean `blocked`/`not-needed` worktrees are removed; dirty ones are retained without done history or a GC CompletionReceipt. Worktree-free reservations are policy subjects and are never passed to worktree removal.

Replay receipts and downstream context remain after active removal. Each policy uses a stable `(repository, generation, completion_id, policy_kind)` operation ID, saved with its decision and retry count before effects. Labels and close are reconciled against live state; comments use operation markers. Review-timeout retries retain pending/count/backoff state, and base-branch-red preserves attempt/marker/escalation behavior. Local not-needed subjects are closed; cloud and unclaimed subjects require independent review before close or dependency completion.

`--no-apply` is a read-only preview and creates no locks. Apply mode can update Issue labels, comments and close state, and launch independent reviews. Without a running Dispatcher, rerun `orchestune gc` to advance pending policies; cloud reviews require `ORCHESTUNE_ROUTINE_ID` and `ORCHESTUNE_ROUTINE_TOKEN`. With no review provider or an unknown launch result, the review remains pending. A saved launch/attempt ID is reconciled when the provider supports lookup; unknown launches are never started twice. Unresolved launches escalate to human review after the configured review timeout; the policy and dependency remain pending. Review verdict comments carry the exact policy operation marker; generic labels alone do not approve a generation.

All writers using the same resolved state path share its reentrant ledger lock across bounded remote operations and durable saves; physical removal also holds the per-worktree claim lock. Different state paths do not share that exclusion boundary. Running/current worktrees and ownership mismatches remain protected. If GC reports `current_worktree`, rerun from the primary checkout. Old `handed_off_to_gc` records are held for explicit evidence migration rather than automatically promoted; resume a recoverable publication through `complete` with its original completion ID and matching claim marker.

| Option | Default | Description |
| :--- | :--- | :--- |
| `--no-apply` | disabled | Show decisions without applying them. |
| `--state <path>` | primary checkout's `run_state.json` | Select the ledger path. Relative paths are based on the primary checkout. |
| `--timeout <seconds>` | `0` | Lock wait time for the ledger and claim lock. Negative and non-finite values are argument errors. |

## Local claim recovery (`orchestune recover`)

Local claim resume, footprint amendment and completion no longer read or write
owner token files. They verify the caller's claim generation, Git common directory,
registered worktree and checked-out branch. Existing token files can remain;
`owner_token_digest` in old state/journals remains readable as compatibility metadata,
and no bulk state migration is needed. Routine API authentication is unchanged.

A marker identifies a worktree generation, not an OS process. Stop old agents
before reusing that worktree: a process that reads a replaced marker cannot be
distinguished from the new agent. Service callers should keep the expected claim
ID captured at launch; that old ID remains rejected after reassignment.

Run diagnosis from the primary checkout. Preview is read-only:

```bash
orchestune recover --issue <N>
orchestune recover --issue <N> --claim-id <ID> --reason "worker stopped" --apply
# Repair a missing marker before resuming a pending completion:
orchestune recover --issue <N> --claim-id <ID> --reason "marker lost" --restore-marker --apply
```

Use the claim ID from diagnosis. Apply repeats checks under the shared state lock
and worktree lock. Live workers, uncertain/external launches, changed generations,
foreign repositories and unfinished completion publication are held. Reconcile
uncertain launches with Dispatcher; restore the marker and resume publication with
its completion ID. Both interactive and dispatch claims are supported once stopped.
`--state <path>` selects the same ledger used by Dispatcher.

Release removes only that active reservation and saves a durable recovery receipt
with its generation and reason. It retains dirty work, commits, worktree, branch,
other claims, counters, intents and completion evidence. It leaves GitHub state
unchanged; use the existing engine/Outcome lifecycle for requeue, completion or
closure. Dispatcher does not resurrect the explicitly released generation.
Repeated release is idempotent. Do not delete the entire `run_state.json`.

### Operator-confirmed external stop

First stop the execution on the provider side, verify its execution ID, terminal
state and artifacts, and keep it stopped. From the primary checkout, preview:

```bash
orchestune recover --issue <N> --claim-id <CLAIM> --external-id <EXECUTION> \
  --launch-attempt-id <ATTEMPT> --confirm-external-stopped \
  --reason "execution URL and verified stop result"
# Add --apply to the same arguments after inspecting the preview.
```

Claim/external IDs and a nonblank reason are required even for preview. Supply
the exact attempt ID when the active ledger has one; omit the option when it
does not. This command sends no stop/cancel request, kills no PID, and cannot be
combined with `--restore-marker`. It retains worktree, branch, marker and GitHub
state. `--state` follows the usual shared-workspace path rules.

Inspect `runtime_state`, `stop_evidence_source` and
`provider_observation_reason`: fresh `running` always refuses recovery, `stopped`
uses provider evidence, and `unknown` uses the operator's confirmation. Missing
authentication, unsupported lookup or unproven provider identity is unknown;
invalid configuration refuses with `provider_config_invalid`. Apply independently
rechecks identity, PID, marker, completion and runtime under locks.

With no current completion, apply atomically records confirmation and release,
removes only the active entry, and returns `external_stop_confirmed_released`.
Pending completion, handed-off completion or a current-generation journal keeps
the entry with `external_stop_confirmed_active_retained`; resume the existing
completion (journal-only reports `completion_resume_required`) or let GC collect
a verified handoff. `completion_state_invalid` requires the usual publication
investigation; do not replace that with ledger edits, label changes or force release.
After release, use the normal requeue or completion/PR integration workflow.

Preview actions are `would_external_stop_confirm_release` and
`would_external_stop_confirm_active_retained`. Replays keep the first reason,
timestamp and snapshot, returning `already_external_stop_confirmed_released`,
`already_external_stop_confirmed_active_retained`, or
`already_external_stop_confirmed_active_absent` if another lifecycle removed the
entry. The last result does not assert that recover released it. A new execution
generation or ambiguous/corrupt evidence refuses. Success exits 0, refusal 43;
invalid external argument combinations are argument errors.

GC can use the saved operator confirmation only for unknown runtime and the same
repository and fixed execution identity. Completion progress does not invalidate
it; ownership, claim time, branch, attempt or start-time changes do. It has no TTL
and never overrides a fresh running observation. Normal Outcome, merge, ownership,
completion and WIP preservation requirements still apply.

A merged PR can be completed from its claim worktree with the usual `complete`
command when Issue, claim creation time, head, expected base, repository, merge
reachability and reopening history match. CI and publication requirements still
apply, including merges into a parent branch. Missing proof holds completion.


## Launch control defaults and migration (#1154)

`max-concurrent` (default `2`, counted from `active_worktrees` including interactive
claims) is now the primary launch control. `max-launches-per-window` is unset by
default, so launches are no longer throttled to one per hour. Launch history is still
recorded while unset, so setting `0` or a positive value later takes effect immediately
against recent launches. Setting it back after the history was trimmed by a shorter
window does not restore dropped records.

To keep the former behavior ("1 launch per hour", token aggregation over 1 hour, no
timeout), set all three explicitly:

```toml
max-launches-per-window = 1
window-seconds = 3600
task-timeout-seconds = 0
```

Setting `max-launches-per-window` while omitting `window-seconds` now means "per 2 hours".
`window-seconds` also drives `max-tokens-per-window` aggregation, aging normalization and
launch-history retention. A limit of `0` only stops new launches; GC, timeouts and label
synchronization keep running. To stop everything, stop the dispatcher itself.

### External executions are held until stopped

The GC releases a slot for an external (cloud) execution when the provider reports
a non-resumable terminal state, or unknown runtime has a valid operator confirmation
for the exact execution generation. PR/Outcome state (merged/closed PR,
handoff-ready) says whether the *work* is complete, not whether the cloud run can still
execute code. When the runtime state is running, unknown, unsupported, or the lookup
fails, the GC keeps `active_worktrees` and the execution handle, sends the Issue to
`status:blocked-human-review`, and never auto-requeues it. This applies to timeouts,
stale-ledger cleanup after `status:in-progress` is removed, and completion collection.
When the work is already complete (merged PR or Outcome) but the run is not confirmed
stopped, the GC keeps the completion result labels and instead posts one Issue comment
explaining the held slot (it is posted again only when the reason changes).
Currently only Codex Cloud can report a stopped state (`ready`, `applied` and `error`
from `codex cloud list` are stopped; `pending` is running); other external targets (for
example Cloud Routine) remain held without operator confirmation, including after
they finish. Check the cloud-side run and artifacts, stop it there if needed, and
use the operator-confirmed external recovery procedure above.
