# Task Claim and Worktree Preparation (Step 2.5)

Before claim, follow [Initial Footprint](../SKILL.md#initial-footprint-before-issue-creation-or-claim) for new and existing Issues.

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
4. If it reports `existing_claim_unrecovered` for a task you hold, follow the printed next actions: continue in the reported worktree, `--resume <claim_id>`, or `--amend-footprint` when files outside the reservation are needed.
5. For unresolved dependencies or conflict rejections, resolve the conflicting task or wait until dependencies complete before retrying.

## Worktree completion and retention

For dispatcher-launched (`owner_kind=dispatch`) and claimed interactive
(`owner_kind=interactive`) worktrees, run `orchestune complete` for the task
outcome. It posts the fixed Outcome, confirms its result label, saves durable
handoff/replay evidence, and preserves the worktree. Do not remove
the worktree as part of completion. The dispatch cycle handles dispatcher
worktrees. For an interactive task after its PR is merged, run the local GC
command from the primary checkout:

```bash
orchestune gc --no-apply
orchestune gc
```

Review the preview before applying it. Standalone GC physically collects interactive
claims; Dispatcher collects dispatch worktrees through the same guarded collector.
Both discover label-confirmed journal/receipt generations and pending policies even
without active entries or in-progress labels. GC can change Issue labels/comments,
close approved subjects, and launch independent not-needed review. Without Dispatcher,
rerun GC; cloud review needs routine credentials. Unknown launch results remain pending
unless the provider can reconcile the saved attempt ID; never launch again blindly. Unresolved launches escalate to human review after the configured review timeout; the policy and dependency remain pending.
Independent approval uses the exact operation marker, not a generic result label.

Save pending policy context and replay receipts before active/claim collection.
`done` requires matching Outcome and merged PR/head/base/owner evidence. Dirty
blocked/not-needed worktrees remain; unapproved not-needed produces no GC completion
receipt or dependency completion. Worktree-free reservations never enter removal.
The same resolved state path shares a reentrant ledger lock across bounded remote
operations and saves; physical GC also holds the claim lock. Different state paths
are separate exclusion domains. Legacy `handed_off_to_gc` stays held until verified
migration; recover publication with the original completion ID and credentials.

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
Editing an Issue body does not shrink or expand an already acquired reservation. For an already claimed task, proceed to Step 2.6 and reconcile the declaration with the held reservation; do not release other tasks' reservations or write outside the held scope. If additional files are needed beyond a file reservation, stop editing them. For an interactive claim, add them to the Issue `footprint` and run `orchestune claim <issue_number> --amend-footprint` (preview with `--no-apply`). It widens the held reservation to the Issue footprint plus files already changed in the worktree, or reports the conflicting task and changes nothing; on conflict, revert the overlapping changes or wait for that task. Never use it to shrink scope or take files another task holds. For a dispatcher-launched task, `--amend-footprint` is rejected; do not edit the Issue footprint, and run `orchestune complete --issue <issue_number> --result blocked --reason footprint-expansion-required` instead.
