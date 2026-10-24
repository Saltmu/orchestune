# Operator completion and GC lifecycle

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
migration; recover publication with the original completion ID and matching claim marker.

A retained worktree keeps its claim marker, and GC saves a `gc-release` recovery
receipt in the same write that removes the active entry. The next claim of that
Issue reuses the same worktree, branch, HEAD, index and uncommitted files only after
it matches the receipt with the completion journal and replay evidence, confirms the
released execution has stopped, and finds every retained change inside its own
footprint. It never resets, cleans, stashes or commits the retained work, and the
rollback of a failed launch never deletes the reused worktree or branch. The receipt records the
takeover as `prepared`, `active_saved` and `completed`, so an interrupted claim resumes
with the same old and new generations. Without that evidence claim stops with
`claim_id_mismatch`, naming the old claim, branch, path and missing evidence for
manual review; do not rewrite the marker by hand. Released generations, including
GC-released ones, are never restored from launch journals or labels.

