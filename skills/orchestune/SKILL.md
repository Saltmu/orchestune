---
name: "orchestune"
description: "Single entry-point skill for a 'big rock' (large task): decompose it into subtasks, build/validate a dependency DAG, iterate with the user until approved, then hand off to orchestune-dispatch for Issue creation and dispatch."
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

This is the **single user-facing entry point** for Orchestune. It understands a "big rock" (large-scale development task) presented by a user, decomposes it into subtasks (Decomposition), calculates and validates the dependency graph (DAG) via the `orchestune-dag` CLI, and iterates with the user until the plan is approved. Once approved, it hands off to the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md), which creates the GitHub Issues and configures the dispatcher — the user never needs to invoke that skill directly.

## Trigger Conditions

Load this skill **when a user presents a 'big rock' task and requests task decomposition, implementation roadmap creation, or parallel development.** This is the only skill a user needs to invoke to go from task description to running parallel dispatch — do not ask the user to separately invoke `orchestune-dag` or `orchestune-dispatch`; drive both internally as described below.

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
   parent_issue_number: null  # filled in by orchestune-dispatch once the parent issue exists
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
       shared_contract: null  # e.g. "format-registry" — tag subtasks sharing an unestablished extension point (see Stage 1 "Shared-contract gate")
       writes_shared_contract: false  # true if this subtask's footprint writes to the shared_contract file under a name orchestune-dag's category patterns won't recognize (usually unnecessary — footprint matches are auto-detected)
       issue_number: null  # filled in by orchestune-dispatch once this subtask's issue exists
   ---

   # Decomposition Plan
   (The section below the frontmatter is free text to explain the design approach or background)
   ```

   The top-level `title` is required — `orchestune-dispatch` uses it to create the parent tracking issue for the whole "big rock" (see that skill's Stage A). `parent_issue_number` and each subtask's `issue_number` start as `null`; do not set them yourself — `orchestune-dispatch` writes the created (or reused) issue numbers back into this file so that re-running the workflow after a partial failure reuses the same issues instead of creating duplicates.

### Stage 2: Validate DAG

1. Delegate consistency validation of the `decomposition_plan.md` to the `orchestune-dag` CLI (this is the "ask orchestune-dag to decompose/validate" step — `orchestune` never re-implements DAG validation itself):

   ```bash
   poetry run orchestune-dag --plan decomposition_plan.md
   ```

   * If validation errors (such as circular dependencies `DagCycleError`) occur, revise `decomposition_plan.md` and re-run this command until it passes.
   * If the output includes a `Warnings:` section, `orchestune-dag` has detected two or more subtasks that both actually *write* to the same shared extension point and are not ordered relative to each other in the DAG (neither is reachable from the other via `depends_on`/inferred edges — having a common ancestor task is not enough, since siblings of a shared ancestor can still run in parallel). "Both write to it" is checked two ways, and either can trigger the warning: (a) subtasks tagged with the same `shared_contract` where each is judged a writer per the footprint/`writes_shared_contract` check in Stage 1, or (b) regardless of tagging — including a tagged subtask paired with one that was never tagged at all, e.g. a declaration was simply missed — any subtasks whose declared `footprint` entries fall into the same shared-extension-point category *and* directory (registry, CLI wiring, public API index, dependency manifest). Pairs already flagged by (a) aren't re-flagged by (b). Tagged subtasks that only depend on the contract without writing to it (pure consumers) are never part of this warning. This is not a blocking error, but it should normally be resolved by revising `decomposition_plan.md` (add a `depends_on` edge directly between the affected writer subtasks, turn a writer into a pure consumer if it doesn't actually need to touch the shared file, add the missing `shared_contract` tag, or confirm the paths genuinely refer to unrelated files) before moving to Stage 3.

### Stage 3: Present Plan and Iterate with the User

1. Organize the validation results of `orchestune-dag` (topological order, parallel leaf subtasks, conflict risks, etc.) and present them to the user.
2. Ask for approval. If the user requests changes instead (feedback), revise `decomposition_plan.md` accordingly and return to **Stage 2** to re-validate — repeat this loop until the user explicitly approves the plan.

### Stage 4: Hand Off to Dispatch

1. Once the user approves the plan, load and follow the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md) with the approved `decomposition_plan.md` as input. That skill is responsible for creating the GitHub Issues (with `Footprint` metadata and `status`/`priority`/`risk` labels) and running the `orchestune-dispatch` CLI to configure/start dispatch.
2. Report the outcome (created Issues, dispatch status) back to the user to conclude the task.
