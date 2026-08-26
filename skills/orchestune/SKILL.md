---
name: "orchestune"
description: "Single entry-point skill for a 'big rock' (large task): decompose it into subtasks, build/validate a dependency DAG, iterate with the user until approved, then hand off to orchestune-provision for Issue creation and orchestune-dispatch for parallel dispatch."
version: "1.0.0"
category: "Development"
input_schema:
  type: "object"
  properties: {}
output_schema:
  type: "object"
  properties: {}
---

# Orchestune Core Skill

This is the **single user-facing entry point** for Orchestune. It understands a "big rock" (large-scale development task) presented by a user, decomposes it into subtasks (Decomposition), calculates and validates the dependency graph (DAG) via the `orchestune-dag` CLI, and iterates with the user until the plan is approved. Once approved, it hands off to the [orchestune-provision skill](../orchestune-provision/SKILL.md) for GitHub Issue creation, and optionally to the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md) for parallel dispatch — the user never needs to invoke those skills directly.

> [!NOTE]
> **User-Facing Response Language**:
> While this skill instruction is written in English, all user-facing explanations, plans, questions, and responses must use the user's preferred language (e.g., Japanese if the user interacts in Japanese or matches the user's environment). The language of this instruction document must not determine the output language.

## Trigger Conditions

Load this skill **when a user presents a 'big rock' task and requests task decomposition, implementation roadmap creation, or parallel development.** This is the only skill a user needs to invoke to go from task description to running parallel dispatch — do not ask the user to separately invoke `orchestune-dag`, `orchestune-provision`, or `orchestune-dispatch`; drive them internally as described below.

**Do not load it for small tasks.** Orchestune optimizes the finished work produced per unit of AI usage quota, not wall-clock speed on a single task; decomposition, Issue provisioning, and dispatch all cost quota of their own. If the task yields fewer than roughly three genuinely independent subtasks, implementing it directly is both faster and cheaper — say so and implement it directly instead.

## Prerequisites

* The `poetry run orchestune-dag` or `orchestune-dag` command must be installed on the system.

## Workflow

### Stage 1: Analyze Task and Create Decomposition Plan

1. Survey the current repository codebase and directory structure to understand the task requested by the user.
2. Identify which modules and files need to be modified (`footprint`), and what classes or functions need to be created or modified (`symbols`).
3. Decompose the task into subtasks that can be executed in parallel independently, or that are logically sequenced.
4. **Shared-contract gate (greenfield decomposition)**: When the "big rock" targets a greenfield area of the repository (new package, new plugin/adapter system, etc.), explicitly look for shared extension points that multiple subtasks are likely to touch even though the file doesn't exist yet — e.g. a plugin/format registry, CLI registration/wiring module, or a dependency manifest. If two or more subtasks would need to establish or edit the same such extension point:
   - Create a dedicated `shared-contract` / `integration-scaffold` subtask that owns those files (creates the registry module, defines the interface/contract).
   - Tag every subtask that plugs into that contract — including the owning subtask itself — with the same `shared_contract: <id>` value (a short slug you choose, e.g. `format-registry`). This is the authoritative signal `orchestune-dag` uses to group them; it does not depend on the subtasks agreeing on a literal file path.
   - The tag alone only means "participates in this contract," not "writes to the shared file" — `orchestune-dag` only compares subtasks that actually *write* to it (their own `footprint` contains a path matching a shared-extension-point pattern, or they explicitly set `writes_shared_contract: true`). Prefer designing dependents as pure consumers: keep their `footprint` limited to their own adapter implementation and tests, and let them only read/import the contract the owning subtask created. Tagged consumers that never touch the shared file are never compared against each other and don't need to be mutually ordered.
   - If two or more subtasks *do* need to write to the shared file themselves (not just the owner), make sure they're actually *ordered* relative to each other, not just each dependent on the owner: `csv` and `yaml` both `depends_on: [shared-contract]` but not on each other can still run in parallel and race on the file. Add an explicit `depends_on` between them (e.g. `yaml` also `depends_on: [csv]`) if they truly must both edit it.
   This is a distinct failure mode from ordinary footprint overlap (see Stage 2): the shared file is often *absent* from every subtask's declared `footprint` in the first place, since it doesn't exist yet and each subtask may independently assume a different name/path for it — so `orchestune-dag`'s similarity-based overlap detection cannot catch it by itself. Declaring and tagging the shared-contract subtask up front is the primary defense; `orchestune-dag`'s hotspot-category warning (Stage 2) is a secondary, heuristic safety net that only catches same-directory naming mismatches, not the `shared_contract` tag's full coverage.
5. Create a `decomposition_plan.md` in the repository root. Use the YAML frontmatter format as follows:

   ```markdown
   ---
   title: "One-line summary of the 'big rock' itself"
   parent_issue_number: null  # or <number> if decomposing an existing issue
   parent_issue_source: derived  # "adopted" if decomposing an existing issue, "derived" if creating a new EPIC
   subtasks:
     - id: task-a
       description: "Implement feature XX"
       overview: "Detailed overview of what feature XX should do."
       proposed_changes:
         - "Modify src/foo.py to add feature XX"
       acceptance_criteria:
         - "Must handle edge case YY"
         - "Must be tested"
       verification_plan:
         - "Run pytest tests/test_foo.py"
       footprint:
         - src/foo.py
       symbols:
         - foo.Foo
       depends_on: []
       priority: medium    # high, medium, low (default: medium)
       risk: false         # true if touching API keys, credentials, or risky subprocesses
       execution_profile: null  # e.g. "fast-code", "deep-reasoning" (abstract execution profile name)
       shared_contract: null  # e.g. "format-registry" — tag subtasks sharing an unestablished extension point (see Stage 1 "Shared-contract gate")
       writes_shared_contract: false  # true if this subtask's footprint writes to the shared_contract file under a name orchestune-dag's category patterns won't recognize (usually unnecessary — footprint matches are auto-detected)
       issue_number: null  # filled in by orchestune-provision once this subtask's issue exists
   ---

   # Decomposition Plan
   (The section below the frontmatter is free text to explain the design approach or background)
   ```

   The top-level `title` is required — `orchestune-provision` uses it to create the parent tracking issue for the whole "big rock" (when `derived`). When decomposing an **existing, pre-filed Issue** (e.g. invoked via `/orchestune <Issue URL>`), set `parent_issue_number: <number>` and `parent_issue_source: adopted` in the plan frontmatter from the start; `orchestune-provision` will adopt this issue as the EPIC parent by default without requiring an explicit `--parent-issue` flag. For greenfield tasks without an existing issue, leave `parent_issue_number: null` and `parent_issue_source: derived` (or omitted); `orchestune-provision` creates a new parent issue and writes back the resolved numbers and `parent_issue_source: derived`. In both cases, the complete plan is synchronized into the parent issue body (`<!-- orchestune:decomposition-plan -->`), ensuring safe persistence even if the local working file is deleted when a worktree is cleaned up. `--parent-issue <number>` can still be passed to `orchestune provision` as an explicit override flag.

### Stage 2: Validate DAG

1. Delegate consistency validation of the `decomposition_plan.md` to the `orchestune-dag` CLI (this is the "ask orchestune-dag to decompose/validate" step — `orchestune` never re-implements DAG validation itself):

   ```bash
   poetry run orchestune-dag --plan decomposition_plan.md
   ```

   * If validation errors (such as circular dependencies `DagCycleError`) occur, revise `decomposition_plan.md` and re-run this command until it passes.
   * A single `Warnings:` output can combine more than one of the following warning types at once — check each entry against its own wording below rather than assuming they're all the same kind:
     - **Shared-contract warning**: `orchestune-dag` has detected two or more subtasks that both actually *write* to the same shared extension point and are not ordered relative to each other in the DAG (neither is reachable from the other via `depends_on`/inferred edges — having a common ancestor task is not enough, since siblings of a common ancestor can still run in parallel). "Both write to it" is checked two ways, and either can trigger the warning: (a) subtasks tagged with the same `shared_contract` where each is judged a writer per the footprint/`writes_shared_contract` check in Stage 1, or (b) regardless of tagging — including a tagged subtask paired with one that was never tagged at all, e.g. a declaration was simply missed — any subtasks whose declared `footprint` entries fall into the same shared-extension-point category *and* directory (registry, CLI wiring, public API index, dependency manifest). Pairs already flagged by (a) aren't re-flagged by (b). Tagged subtasks that only depend on the contract without writing to it (pure consumers) are never part of this warning. This is not a blocking error, but it should normally be resolved by revising `decomposition_plan.md` (add a `depends_on` edge directly between the affected writer subtasks, turn a writer into a pure consumer if it doesn't actually need to touch the shared file, add the missing `shared_contract` tag, or confirm the paths genuinely refer to unrelated files) before moving to Stage 3.
     - **Existence-verification warning** (#393/#400): an entry indicating missing paths in footprint or missing symbols in codebase. This is a distinct check from the shared-contract warning above — `orchestune-dag` checked whether the declared path/symbol is actually present in today's codebase, not whether it collides with another subtask. Do not treat it as a shared-contract warning (it is not about ordering two writers), and do not silently skip past it either — triage each occurrence:
       * **The subtask is about to create this path/symbol for the first time**: no action needed, but the two kinds of entries behave differently — a not-yet-existing `footprint` path is *always* reported as missing (path existence is checked directly against disk), while a not-yet-existing `symbols` entry is reported as missing only when symbol verification actually ran. It runs only if **all** of the following hold: the subtask's `footprint` includes at least one entry that (a) is an existing `.py` file, and (b) that file parses successfully — and *no* existing `.py` file in the footprint fails to parse (even one file with a syntax/encoding error means verification is silently skipped for the whole subtask, not just that file). Any other case — footprint entries that are all brand-new files, footprint containing only non-Python files, or an unparseable existing `.py` file anywhere in the footprint — means symbol verification never ran: no `symbols` warning will appear regardless of whether the declared symbols actually exist, and that silence must not be read as "the symbol was checked and confirmed."
       * **The path/symbol should already exist**: it's most likely a typo or a wrong path/name in `decomposition_plan.md`. Fix the `footprint`/`symbols` entry and re-run Stage 2.
       * **You actually intended to touch a different, already-existing file**: suspect a missing `footprint` declaration rather than a typo — the file you meant to edit may be one another subtask also writes to, and the omission from this subtask's `footprint` is exactly why the Stage 1 shared-contract gate and this same overlap detection never caught the collision. Add the correct path to `footprint` (and reconsider whether a `shared_contract` tag or `depends_on` edge is now needed) before re-validating.
     - **Cycle-resolution warning** (e.g., informational message stating that a similarity cycle was automatically resolved): purely informational, indicating that a similarity cycle was automatically resolved. You do not need to revise the plan for these.
   * You may proceed to Stage 3 once every shared-contract and existence-verification warning above has been triaged (resolved, or confirmed as needing no plan change); cycle-resolution warnings never block moving on.

### Stage 3: Present Plan and Iterate with the User

1. Organize the validation results of `orchestune-dag` (topological order, parallel leaf subtasks, conflict risks, etc.) and present them to the user.
2. Ask for approval. If the user requests changes instead (feedback), revise `decomposition_plan.md` accordingly and return to **Stage 2** to re-validate — repeat this loop until the user explicitly approves the plan.

### Stage 4: Hand Off to Provision and Dispatch

1. Once the user approves the plan, load and follow the [orchestune-provision skill](../orchestune-provision/SKILL.md) with the approved `decomposition_plan.md` as input. That skill creates the parent and child GitHub Issues (with Footprint metadata and status/priority/risk labels) and synchronizes the plan into the parent issue body via the `orchestune provision` CLI.
2. **Determine next step (default: continue to dispatch)**:
   - **Explicit provisioning-only request**: If the user explicitly instructed to stop after creating Issues (e.g., "stop after filing issues" / "plan or provision only"), report the created Issues back to the user and conclude the task.
   - **Default / execution request**: Otherwise (the default flow promised in Trigger Conditions), immediately hand off to the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md) with the provisioned parent Issue number (`--parent-issue <N>`) to configure and run the dispatcher. Note that `orchestune-dispatch` reconstructs its task graph directly from the child Issues' Footprint YAML and does not require `decomposition_plan.md` to remain present. Report the outcome (created Issues, dispatched tasks) back to the user.
