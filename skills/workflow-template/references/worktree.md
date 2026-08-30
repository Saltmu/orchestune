# Worktree Preparation and Cleanup (Step 2.5)

For every user-requested change or existing Issue fix, create an isolated
worktree beneath the repository root before editing source files. First inspect
the primary checkout with `git status --short`; preserve unrelated changes and
never reuse their branch for the task.

## Create and enter the worktree

From the repository root, fetch the current base and create a task-specific
branch. Derive `<BRANCH_SLUG>` by applying `replace('/', '-')` to `<BRANCH>` so
the worktree path stays flat and filesystem-safe.

```bash
git fetch origin main
git worktree add -b <BRANCH> worktree/<BRANCH_SLUG> origin/main
cd worktree/<BRANCH_SLUG>
<INSTALL_COMMAND>
```

Replace `<INSTALL_COMMAND>` with the project's dependency/bootstrap command
(for example, `poetry install`). Then write `implementation_plan.md`, implement,
test, run local CI, commit, push, create the PR, and handle review feedback from
this directory. If the worktree cannot be created because the target or branch
already exists, inspect it with `git worktree list` and choose a new slug; do not
overwrite an existing task.

## Cleanup

### Tasks managed under Orchestune
For tasks executed under Orchestune management, **do not manually remove the
worktree** upon completing the PR and posting the outcome record. The
Orchestune Dispatcher's GC phase will automatically inspect commits and outcome
records, transition the task to completed, and remove the worktree. Manual
removal before GC can cause the dispatcher to misidentify the task as crashed.

### Standalone tasks outside Orchestune
For standalone tasks outside Orchestune, keep the worktree until the PR and
outcome record are complete. From the primary checkout, confirm the task
worktree is clean and then remove it:

```bash
git worktree remove worktree/<BRANCH_SLUG>
git worktree prune
```

Do not use `--force`; resolve or preserve uncommitted work first.

