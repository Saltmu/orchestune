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
poetry install
```

Write `implementation_plan.md`, implement, test, run local CI, commit, push,
create the PR, and handle review feedback from this directory. If the worktree
cannot be created because the target or branch already exists, inspect it with
`git worktree list` and choose a new slug; do not overwrite an existing task.

## Auto-Dispatch exception

When Orchestune Auto-Dispatch has already launched the task in a
dispatcher-provisioned worktree, skip this step. Use that assigned branch and
worktree for all remaining work; do not create a nested worktree or a different
PR branch.

## Cleanup

Keep the worktree until the PR and outcome record are complete. From the
primary checkout, confirm the task worktree is clean and then remove it:

```bash
git worktree remove worktree/<BRANCH_SLUG>
git worktree prune
```

Do not use `--force`; resolve or preserve uncommitted work first.
