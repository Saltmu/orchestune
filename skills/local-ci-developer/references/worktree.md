# Task Claim and Worktree Preparation (Step 2.5)

Start: inspect `git status` and `git diff` before checkout/overwrite; preserve inherited changes. Before claim, follow [Initial Footprint](../SKILL.md#initial-footprint-before-issue-creation-or-claim) for new and existing Issues.

Actions: use the claim/recovery paths below. Finish: current claim, reservation, worktree and migrated session agree.
Failure: follow diagnostics without discarding work or bypassing reservations. Save working progress in WIP commits at interruptions/session changes.

If you are already inside the task worktree (e.g. launched directly into the
task workspace), the task is already claimed—proceed directly to Step 2.6 to reconcile the declaration with the held reservation; editing the Issue does not change that reservation.

Otherwise, from the repository root, claim the task issue before modifying
source files to validate prerequisites, resolve the base branch, prepare an
isolated worktree, record active state, and update issue labels:

```bash
orchestune claim <issue_number>
```

`orchestune claim` performs preflight validation, fetches the base branch,
creates the isolated task worktree under `worktree/`, updates the task ledger,
and transitions task status labels automatically. Do not manually invoke
`git worktree add` or mutate labels directly. If it succeeds with `Warning: footprint overlaps issue #<N>`, another interactive task reserves some of the same files: continue, record the overlap in the plan and PR body, and rebase and resolve conflicts if that task merges first. Overlap with a dispatch reservation is still rejected.

## Enter the worktree and start work

`orchestune claim` outputs the prepared worktree path upon success:

```bash
cd <worktree_path>
uv sync
```

Create the parent's unique worktree-local `<session-dir>`, migrate `<planning-session-dir>/implementation-plan.md` into it, and use its explicit path. Implement, test, run local CI, commit, push, create the PR, resolve review and run `orchestune complete` entirely in this worktree.

## Claim failures and recovery

If `orchestune claim` fails (non-zero exit code):
1. Review the diagnostic output printed to stderr for the reason and conflicting issues or branches.
2. If already in the task worktree and the command reports `already_in_progress`, the task was already claimed—proceed with the current worktree.
3. If an existing claim was interrupted, resume it using the reported claim ID:
   ```bash
   orchestune claim <issue_number> --resume <claim_id>
   ```
4. If it reports `existing_claim_unrecovered` for a task you hold, follow the printed next actions: continue in the reported worktree, `--resume <claim_id>`, or `--amend-footprint` when files outside the reservation are needed.
5. For unresolved dependencies or conflict rejections, resolve the conflicting task or wait until dependencies complete before retrying.

## Worktree completion and retention

### Restart a completed blocked local task

For an interactive local task whose `blocked` Outcome is already handed off,
a human must first resolve its blocker and set the open Issue to
the in-progress state. From the primary checkout, preview the explicit restart:

```bash
orchestune recover --issue <N> --claim-id <old-claim-id> --reason "blocker resolved" --restart-blocked
```

Inspect the preview, then apply the same request with `--apply`. It preserves the old Outcome/journal/replay receipt, branch/worktree/footprint and assigns a new generation. Continue in the reported worktree without a separate claim. Retry the same old claim ID/reason to reconcile interrupted publication without rotating again; an old `--completion-id` still replays its immutable result. Pending publication, live workers and dispatch/cloud claims cannot restart this way. CI/review gates still apply: require successful CI for the current clean HEAD before done.

Interruption example: re-read the explicit session, claim/Issue, Git status, PR and evidence before resuming. A stage printed before interruption cannot establish current readiness; a changed generation or worktree requires reconciliation.

Complete preserves the worktree. Before GC, read and follow the shared
[operator completion and GC lifecycle](../../../docs/operator-completion-lifecycle.md).

## Initial Footprint Procedure

During Step 1, inspect the request and relevant files to list expected repository-relative file paths, including tests and docs, in the plan. This initial investigation precedes Issue creation and claim; Step 2.6 refines it inside the worktree.
For a new Issue (Step 2), put that list in the Issue body's first fenced `yaml` block under `## Footprint`, as below. Prose or comments alone are not read by claim. Adapt the paths and metadata to the task:

```yaml
subtask_id: issue-footprint-declaration
description: Declare planned files before claim
footprint:
  - skills/local-ci-developer/SKILL.md
  - tests/test_skill_commands.py
symbols: []
depends_on: []
```

For an existing Issue, before Step 2.5 fetch its current body with the selected GitHub backend and check that this YAML is valid and `footprint` covers the expected changes with repository-relative file paths (including planned new files). Add or correct missing, stale, or invalid declarations in the body, preserving unrelated metadata and content, then re-fetch to verify before claim. Skipping Issue creation does not skip this check.
If scope cannot be determined, record the concrete reason in the Issue body and plan and explicitly choose a **repository reservation** by omitting `footprint`; for example, “The failing subsystem is not yet identified; reserve the repository until investigation determines the affected files.” Do not use `footprint: []` to disguise unknown scope or claim a file-scoped reservation. Missing or empty footprints select a repository reservation.
Editing an Issue body does not shrink or expand an already acquired reservation. For an already claimed task, proceed to Step 2.6 and reconcile the declaration with the held reservation; do not release other tasks' reservations or write outside the held scope. If additional files are needed beyond a file reservation, stop editing them. For an interactive claim, add them to the Issue `footprint` and run `orchestune claim <issue_number> --amend-footprint` (preview with `--no-apply`). It widens the held reservation to the Issue footprint plus files already changed in the worktree, or reports the conflicting task and changes nothing; on conflict, revert the overlapping changes or wait for that task. An overlap with another interactive reservation only warns, as for a new claim. Never use it to shrink scope or take files another task holds. For a dispatcher-launched task, `--amend-footprint` is rejected; do not edit the Issue footprint, and run `orchestune complete --issue <issue_number> --result blocked --reason footprint-expansion-required` instead.
