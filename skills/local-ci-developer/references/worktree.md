# Task Claim and Worktree Preparation (Step 2.5)

If you are already inside the task worktree (e.g. launched directly into the
task workspace), the task is already claimed—proceed directly to Step 2.6.

Otherwise, from the repository root, claim the task issue before modifying
source files to validate prerequisites, resolve the base branch, prepare an
isolated worktree, record active state, and update issue labels:

```bash
orchestune claim <issue_number>
```

`orchestune claim` performs preflight validation, fetches the base branch,
creates the isolated task worktree under `worktree/`, updates the task ledger,
and transitions task status labels automatically. Do not manually invoke
`git worktree add` or mutate labels directly.

## Enter the worktree and start work

`orchestune claim` outputs the prepared worktree path upon success:

```bash
cd <worktree_path>
uv sync
```

Create the unique worktree-local `<session-dir>` defined by the parent skill and
migrate the approved plan from `<planning-session-dir>` to
`<session-dir>/implementation-plan.md`. From this point, refer only to the explicit
worktree-local path. Then implement, test, run local CI, commit, push, create
the PR, handle review feedback, and run `orchestune complete` entirely from
within this worktree.

## Claim failures and recovery

If `orchestune claim` fails (non-zero exit code):
1. Review the diagnostic output printed to stderr for the reason and conflicting issues or branches.
2. If already in the task worktree and the command reports `already_in_progress`, the task was already claimed—proceed with the current worktree.
3. If an existing claim was interrupted, resume it using the reported claim ID:
   ```bash
   orchestune claim <issue_number> --resume <claim_id>
   ```
4. For unresolved dependencies or conflict rejections, resolve the conflicting task or wait until dependencies complete before retrying.

## Worktree completion and retention

For dispatcher-launched (`owner_kind=dispatch`) and claimed interactive
(`owner_kind=interactive`) worktrees, run `orchestune complete` for the task
outcome. It posts to the task Issue and preserves the worktree. Do not remove
the worktree as part of completion. The dispatch cycle handles dispatcher
worktrees. For an interactive task after its PR is merged, run the local GC
command from the primary checkout:

```bash
orchestune gc --no-apply
orchestune gc
```

Review the preview before applying it. This command handles handoff-ready
interactive reservations only; the dispatch cycle owns dispatch worktrees. It
verifies the matching Outcome and merged PR for `done` tasks, and retains dirty
worktrees for `blocked` and `not-needed` outcomes. It does not update GitHub or
start a dispatch cycle.
