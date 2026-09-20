# Task Claim and Worktree Preparation (Step 2.5)

Before modifying source files, claim the task issue to validate prerequisites,
resolve the base branch, prepare an isolated worktree, record active state,
and update issue labels:

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

Write `implementation_plan.md`, implement, test, run local CI, commit, push,
create the PR, and handle review feedback entirely from within this worktree.

## Claim failures and recovery

If `orchestune claim` fails (non-zero exit code):
1. Review the diagnostic output printed to stderr for the reason and conflicting issues or branches.
2. If an existing claim was interrupted, resume it using the reported claim ID:
   ```bash
   orchestune claim <issue_number> --resume <claim_id>
   ```
3. For unresolved dependencies or conflict rejections, resolve the conflicting task or wait until dependencies complete before retrying.

## Worktree completion and lifecycle

Do not manually remove the worktree (`git worktree remove`) after submitting
the PR and posting the outcome record. Orchestune manages the task lifecycle
and will clean up the worktree automatically during reconciliation.
