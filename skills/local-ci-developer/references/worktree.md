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

## Branch naming convention (agent-neutral)

Name `<BRANCH>` as `<type>/issue-{N}-{slug}` (e.g. `fix/issue-777-branch-naming`,
`feat/issue-...`, `docs/issue-...`). This is not a `claude`-specific convention:
Orchestune's issue/PR-linking logic (`orchestune.branch_naming`,
`orchestune.pr_link_notice`) recognizes this `<prefix>/issue-{N}-{subtask_id}`
shape regardless of which agent or human created the branch, so any prefix
works as long as the `issue-{N}-` segment is present.

When Orchestune Auto-Dispatch has already assigned and pushed the branch for a
subtask, **use that exact branch name; never rename it or recreate it from an
assumed pattern.** The assigned name is built by
`orchestune.branch_naming.build_task_branch_name()`, whose default prefix stays
`claude` for compatibility with in-flight tasks and with the Claude Code cloud
routine's `claude/`-only branch-push restriction.

## Auto-Dispatch exception

When Orchestune Auto-Dispatch has already launched the task in a
dispatcher-provisioned worktree, skip this step and the cleanup section. Use
that assigned branch and worktree for all remaining work; do not create a
nested worktree, a different PR branch, or remove/prune the dispatcher-owned
worktree.

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

