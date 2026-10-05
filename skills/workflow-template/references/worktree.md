# Worktree Preparation and Retention (Step 2.5)

Before claim, follow [Initial Footprint](../SKILL.md#initial-footprint-before-issue-creation-or-claim) for new and existing Issues.

For every user-requested change or existing Issue fix, use an isolated task
worktree before editing source files. First inspect `git status --short`;
preserve unrelated changes. If already inside the task worktree, proceed there.

## Claim and enter the worktree

Otherwise, from the repository root, claim the Issue. The command validates
dependencies, resolves the base branch, and prepares the task worktree.

```bash
orchestune claim <issue_number>
cd <worktree_path>
<INSTALL_COMMAND>
```

Replace `<INSTALL_COMMAND>` with the project's dependency/bootstrap command
(for example, `uv sync`). Then create the unique `<session-dir>` defined by the
parent skill, write `<session-dir>/implementation-plan.md`, implement,
test, run local CI, commit, push, create the PR, handle review feedback, and run
`orchestune complete` from this directory. If the claim reports an interrupted
reservation, resume it with its reported claim ID instead of creating another
worktree:

```bash
orchestune claim <issue_number> --resume <claim_id>
```

If an interactively claimed task needs files outside its held file reservation,
add them to the Issue `footprint` and run `orchestune claim <issue_number> --amend-footprint`. It widens
the reservation, or reports the conflicting task and changes nothing. Dispatcher-
launched tasks cannot amend; report `orchestune complete --issue <issue_number>
--result blocked --reason footprint-expansion-required` instead.

If a claim or amend succeeds with `Warning: footprint overlaps issue #<N>`, another
interactive task reserves some of the same files. Continue, record the overlap in
the plan and PR body, and rebase and resolve conflicts if that task merges first.
Overlap with a dispatch reservation is still rejected.

## Branch naming convention (agent-neutral)

Use the branch assigned by Orchestune or specified by the task. Do not rename
or recreate an existing task branch.

## Completion and retention

For both dispatcher-managed and interactively claimed worktrees, run `orchestune complete`
for the task outcome. It posts to the task Issue and preserves the worktree.
Do not remove the worktree as part of completion. Orchestune's GC phase handles
the lifecycle transition after handoff, including dispatcher-managed cleanup.

## Initial Footprint Procedure

During Step 1, inspect the request and relevant files to list expected repository-relative file paths, including tests and docs, in the plan.
For a new Issue (Step 2), put that list in the Issue body's first fenced `yaml` block under `## Footprint`, as below. Prose or comments alone are not read by claim:

```yaml
subtask_id: <task-slug>
description: <one-line summary of the change>
footprint:
  - <path/to/changed_file>
  - <path/to/test_file>
symbols: []
depends_on: []
```

Replace placeholders enclosed in `<...>` with the project's repository-relative file paths, including planned tests, docs, and newly created files.
For an existing Issue, before Step 2.5 fetch its current body with the selected backend and check that this YAML is valid and `footprint` covers the expected changes with repository-relative file paths (including planned new files). Add or correct missing, stale, or invalid declarations in the body, preserving unrelated metadata and content, then re-fetch to verify before claim. Skipping Issue creation does not skip this check.
If scope cannot be determined, record the concrete reason in the Issue body and plan and explicitly choose a repository reservation by omitting `footprint`. Do not use `footprint: []` to disguise unknown scope or claim a file-scoped reservation. Missing or empty footprints select a repository reservation.
If the YAML block is corrupted or malformed, `claim` treats it as a repository reservation without raising an error; re-fetch after any edit to verify that the block parses cleanly as YAML.
Editing an Issue body does not shrink or expand an already acquired reservation. If the task is already claimed, reconcile the declaration with the held reservation before editing files. If additional files are needed beyond the held reservation, follow the `--amend-footprint` procedure in [Claim and enter the worktree](#claim-and-enter-the-worktree).
