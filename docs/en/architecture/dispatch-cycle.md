# Dispatch Cycle Reports & Timings

This document describes the cycle report that each applied dispatch cycle posts to the parent Issue, and the timings (`timings`) recorded in cycle records. For the high-level system overview and core design principles, see [Architecture & Design](../architecture.md).

---

## 1. Purpose

`orchestune-dispatch`'s per-run event log (`events.jsonl`) is `.gitignore`d and does not survive between CI runs, so it cannot serve as durable history on its own. To keep dispatch-cycle decisions traceable without depending on that ephemeral log, the cycle report is posted as a comment to the parent Issue.

## 2. When a report is posted

Each *applied* dispatch cycle posts a `## 🤖 Orchestune Dispatch Cycle Report` comment to the configured parent Issue (`--parent-issue`, #396).

Nothing is posted:

- under `--no-apply`, which skips the entire post-cycle block;
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
