---
name: "orchestune"
description: "Core skill to decompose a large task ('big rock') into subtasks, build/validate a dependency DAG, and automatically create GitHub Issues."
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

This skill is designed to automatically understand a "big rock" (large-scale development task) presented by a user, decompose it into subtasks (Decomposition), calculate and validate the dependency graph (DAG), and register them as GitHub Issues.

## Trigger Conditions

Load this skill **when a user presents a 'big rock' task and requests task decomposition, implementation roadmap creation, or issue registration for parallel development.**

## Prerequisites

* The `poetry run orchestune-dag` or `orchestune-dag` command must be installed on the system.
* The GitHub CLI (`gh` command) must be installed and authenticated (`gh auth status`).
  * If `gh` is unavailable, use the GitHub MCP server, or guide the user to create issues manually via the Web UI.

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

1. Validate the consistency of the DAG using the created `decomposition_plan.md`:

   ```bash
   poetry run orchestune-dag --plan decomposition_plan.md
   ```

   * If validation errors (such as circular dependencies `DagCycleError`) occur, modify `decomposition_plan.md` and re-run validation.

### Stage 3: Present Plan and Obtain User Approval

1. Organize the validation results of `orchestune-dag` (topological order, parallel leaf subtasks, conflict risks, etc.) and present them to the user.
2. Obtain the user's approval on the proposed decomposition plan.

### Stage 4: Create GitHub Issues

1. Once approved, create a GitHub Issue for each subtask.
2. Set the title and body of each Issue as follows:
   * **Title**: `[FEAT] <subtask_id>: <summary of description>`
   * **Body**: Embed the following Footprint YAML block at the end of the body so that the Orchestune dispatcher can parse it:

     ```markdown
     ## Footprint
     ```yaml
     subtask_id: <subtask_id>
     footprint:
       - <path/to/file>
     symbols:
       - <class_or_function>
     depends_on:
       - <dep_subtask_id>
     ```
     ```

   * **Labels**:
     * Assign the `status:blocked` label if the dependency (`depends_on`) is not yet resolved (i.e., there are other unresolved subtasks).
     * Assign the `status:queued` label if there are no dependencies or if all dependencies are resolved (parallel leaves).
     * Assign the appropriate priority label: `priority:high`, `priority:medium`, or `priority:low`.
     * Assign the `risk:flagged` label if the subtask has `risk: true`.

3. **Example Issue Creation Command (using GitHub CLI)**:
   ```bash
   gh issue create --title "[FEAT] task-a: Implement foo feature" --body-file /tmp/issue_body.md --label "status:queued,priority:medium"
   ```

4. Once all issues have been created, report the list of created issues to the user and conclude the task.
