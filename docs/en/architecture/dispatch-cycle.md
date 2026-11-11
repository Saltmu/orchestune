# Dispatch Cycle Reports & Timings

This document describes the cycle report that each applied dispatch cycle posts to the parent Issue, and the timings (`timings`) recorded in cycle records. For the high-level system overview and core design principles, see [Architecture & Design](../architecture.md).

---

## 1. Purpose

`orchestune-dispatch`'s per-run event log (`events.jsonl`) is `.gitignore`d and does not survive between CI runs, so it cannot serve as durable history on its own. To keep dispatch-cycle decisions traceable without depending on that ephemeral log, the cycle report is posted as a comment to the parent Issue.

## 2. When a report is posted

Each *applied* dispatch cycle posts a `## 🤖 Orchestune Dispatch Cycle Report` comment to the configured parent Issue (`--parent-issue`, #396).

Nothing is posted:

- in preview mode, which skips the entire post-cycle block;
- for a cycle with nothing to report;
- when no parent Issue is configured.

## 3. What the comment contains

The comment summarizes that cycle's selected tasks, noteworthy `footprint`-deviation events, completions, and promotions.

Deviation events that merely re-report an unchanged steady state (a worktree that is already force-serialized, say) are excluded from both the skip check and the comment body, so the parent Issue is not flooded with an identical comment every cycle.

## 4. When posting fails

Failure handling matches the other post-cycle phases: posting does not raise, and the cycle itself always runs to completion. A failure does still surface as a nonzero `orchestune-dispatch` exit status, failing a typical CI step.

| Failure | Log | Exit code |
| --- | --- | --- |
| An ordinary posting exception (a transient network error, say) | warning | 2 |
| A GitHub authentication failure | error | 1 |

## 5. Timings in cycle records

Applied cycle records also contain `timings` (version 1).

| Item | Meaning |
| --- | --- |
| Recorded values | `elapsed_seconds`, `phases` (each with inclusive `seconds` and interval `count`), and `gh` (`calls` and aggregate `seconds`). |
| Scope | The scope `cycle_before_events_record` spans the existing `cycle: started` notification through `events_record: started`; it excludes event writing, lock release, post-cycle work, and result-file saving. |
| Phase boundaries | Existing progress notifications supply the boundaries. Repeated task launches are aggregated; their duration is already included in scheduling, so phase durations must not be summed as a cycle total. |
| `gh.calls` | Counts attempts through `GitHubForge._run`, including stdin, failures, and timeouts, rather than HTTP requests or commands in child agents. |
| Measurement failures | Produce `collection_status: partial` or `unavailable` and null for unknown scalar values, without changing dispatch results or exit codes. |
| Compatibility | Old records can lack `timings`; result JSON and `CycleReport` remain unchanged. Dry runs and interrupted cycles retain the existing event-writing policy. |

## 6. Dispatcher 4-Stage Execution Pipeline (#1377)

In apply mode, Dispatcher splits each execution into four distinct stages:

1. **Pre-integration cycle (`run_pre_integration_cycle`)**: Runs while holding the `run_state_lock` up through `gc_reclaim` (detecting completions, reclaiming worktrees, running consistency checks) and records cycle events (`stage="pre_integration"`). Task selection, launching, and external-lock scanning are skipped. Releases `run_state_lock` upon completion.
2. **Integration boundary (`pre_scheduling_integration`)**: Runs outside the `run_state_lock`. Handles auth check, polls pending `status:not-needed` reviews, advances layer-2 semantic review, and runs `Integrator` once. Because `run_state_lock` is not held during synchronous CI runs, other agent completions are not blocked.
3. **Scheduling cycle (`run_dispatch_cycle`)**: Acquires `run_state_lock` with a fresh context, evaluates `reconciliation_external_locks` (external-lock reconciliation and dependency promotions) and performs `scheduling` (selection and launching), recording events (`stage="scheduling"`). Releases `run_state_lock` upon completion.
4. **Post-cycle (`_post_cycle_steps`)**: Runs outside `run_state_lock`. Processes parent completion, posts a combined event log comment to the parent Issue merging events from both cycles, and notifies finding notices from consistency evaluation.

In preview mode, the pre-integration cycle and integration boundary are skipped; only a single scheduling cycle runs FULL without side-effect writes.

## 7. Filtering Candidates for Active Reservation Overlaps (#1547)

During candidate selection in the scheduling cycle (`_filter_candidates_for_forced_serial`), queued tasks whose Footprint overlaps with any active dispatch-owned reservation (Issue Footprint ∪ `declared_footprint`) or repository reservation are excluded from launch candidates.

Excluded tasks remain in `status:queued` for the next cycle (recorded as `forced-serial` in skip records) and are dispatched safely once the preceding reservation is released upon task completion. This prevents unneeded `status:blocked` transitions from `CLAIM_CONFLICT` at claim time when active tasks widen their reservations via `--amend-footprint`. Consistent with claim conflict rules, overlaps with interactive reservations do not cause exclusion.

## 8. GitHub reads inside one dispatch (#1836)

In apply mode, the pre-integration cycle and the scheduling cycle each rebuild context with `_build_cycle_context`: child Issues, the open PR list including files, and candidate branch presence. The integration boundary releases `run_state_lock`, so scheduling does not reuse the pre-integration reads.

Candidate branches are the canonical names that have a verified candidate in that context. They are checked together, at most 50 per GraphQL call. A Forge without the batch operation still calls `branch_exists` once per candidate. Recovery and the Integrator's single-ref checks stay one branch at a time. An indeterminate candidate is not treated as absent and does not fall back to a PR branch.

External-lock checks use the open PR list that includes files. Attempt lookup, reusable integration-PR matching, and the resolver list in integration preparation use a lightweight list without files. Attempt finalization reads the list again immediately before it reconciles.

Parent completion decides legacy final-PR migration before it discovers child Issues. When that migration does not write, the pending marker receives the candidate already read. When the body is rewritten, the pending marker reads the lightweight list again. A failed migration returns `unsafe_final_pr` and does not mark the PR pending. Standalone pending, migration, and integration-PR calls that omit the list still fetch it themselves when they need it.


