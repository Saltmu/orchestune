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
4. Create a `decomposition_plan.md` in the repository root. Use the YAML frontmatter format as follows:

   ```markdown
   ---
   subtasks:
     - id: task-a
       description: "Implement feature XX"
       footprint:
         - src/foo.py
       symbols:
         - foo.Foo
       depends_on: []
       priority: medium  # high, medium, low (default: medium)
       risk: false       # true if touching API keys, credentials, or risky subprocesses
   ---

   # Decomposition Plan
   (The section below the frontmatter is free text to explain the design approach or background)
   ```

### Stage 2: Validate DAG

1. Delegate consistency validation of the `decomposition_plan.md` to the `orchestune-dag` CLI (this is the "ask orchestune-dag to decompose/validate" step — `orchestune` never re-implements DAG validation itself):

   ```bash
   poetry run orchestune-dag --plan decomposition_plan.md
   ```

   * If validation errors (such as circular dependencies `DagCycleError`) occur, revise `decomposition_plan.md` and re-run this command until it passes.

### Stage 3: Present Plan and Iterate with the User

1. Organize the validation results of `orchestune-dag` (topological order, parallel leaf subtasks, conflict risks, etc.) and present them to the user.
2. Ask for approval. If the user requests changes instead (feedback), revise `decomposition_plan.md` accordingly and return to **Stage 2** to re-validate — repeat this loop until the user explicitly approves the plan.

### Stage 4: Hand Off to Dispatch

1. Once the user approves the plan, load and follow the [orchestune-dispatch skill](../orchestune-dispatch/SKILL.md) with the approved `decomposition_plan.md` as input. That skill is responsible for creating the GitHub Issues (with `Footprint` metadata and `status`/`priority`/`risk` labels) and running the `orchestune-dispatch` CLI to configure/start dispatch.
2. Report the outcome (created Issues, dispatch status) back to the user to conclude the task.
