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
4. **Shared-contract gate (greenfield decomposition)**: When the "big rock" targets a greenfield area of the repository (new package, new plugin/adapter system, etc.), explicitly look for shared extension points that multiple subtasks are likely to touch even though the file doesn't exist yet — e.g. a plugin/format registry, CLI registration/wiring module, or a dependency manifest. If two or more otherwise-parallel subtasks would need to establish or edit the same such extension point:
   - Create a dedicated `shared-contract` / `integration-scaffold` subtask that owns those files (creates the registry module, defines the interface/contract).
   - Make every subtask that plugs into that contract declare `depends_on: [<shared-contract-subtask-id>]`, rather than leaving them as independent parallel leaves.
   - Keep each feature subtask's own footprint limited to its adapter implementation and tests wherever possible.
   This is a distinct failure mode from ordinary footprint overlap (see Stage 2): the shared file is often *absent* from every subtask's declared `footprint` in the first place, since it doesn't exist yet and each subtask may independently assume a different name/path for it — so `orchestune-dag`'s similarity-based overlap detection cannot catch it by itself. Declaring the shared-contract subtask up front is the primary defense; `orchestune-dag`'s hotspot-category warning (Stage 2) is a secondary safety net for when the extension point *is* declared but under inconsistent names.
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
   * If the output includes a `Warnings:` section, `orchestune-dag` has detected multiple subtasks whose declared `footprint` entries fall into the same shared-extension-point category (registry, CLI wiring, public API index, dependency manifest — see the shared-contract gate in Stage 1) without any explicit or inferred dependency connecting them. This is not a blocking error, but it should normally be resolved by revising `decomposition_plan.md` (introduce a `shared-contract` subtask and `depends_on` edges, or confirm the paths genuinely refer to unrelated files) before moving to Stage 3.

### Stage 3: Present Plan and Iterate with the User

1. Organize the validation results of `orchestune-dag` (topological order, parallel leaf subtasks, conflict risks, etc.) and present them to the user.
2. Ask for approval. If the user requests changes instead (feedback), revise `decomposition_plan.md` accordingly and return to **Stage 2** to re-validate — repeat this loop until the user explicitly approves the plan.

### Stage 4: Hand Off to Dispatch

1. Once the user approves the plan, load and follow the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md) with the approved `decomposition_plan.md` as input. That skill is responsible for creating the GitHub Issues (with `Footprint` metadata and `status`/`priority`/`risk` labels) and running the `orchestune-dispatch` CLI to configure/start dispatch.
2. Report the outcome (created Issues, dispatch status) back to the user to conclude the task.
