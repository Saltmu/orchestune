# Worktree Preparation and Retention (Step 2.5)

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
worktree.

## Branch naming convention (agent-neutral)

Use the branch assigned by Orchestune or specified by the task. Do not rename
or recreate an existing task branch.

## Completion and retention

For both dispatcher-managed and interactively claimed worktrees, run `orchestune complete`
for the task outcome. It posts to the task Issue and preserves the worktree.
Do not remove the worktree as part of completion. Orchestune's GC phase handles
the lifecycle transition after handoff, including dispatcher-managed cleanup.
