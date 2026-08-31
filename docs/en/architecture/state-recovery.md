# Stateless CI & Self-Healing State Recovery

This document provides detailed specifications for Orchestune's stateless execution model, self-healing state recovery from GitHub as the single source of truth, reclaim count management, and the repository consistency control loop. For the high-level system overview and core design principles, see [Architecture & Design](../architecture.md).

---

## 1. Stateless CI Execution Model & Self-Healing

Orchestune's dispatcher is designed to run in **stateless CI environments (such as GitHub Actions)** where local workspaces are destroyed at the end of each run.

Typically, orchestrator states are tracked in a local state file like `run_state.json`. If this file is lost, Orchestune reconstructs the state using the following **self-healing** flow:

```text
[Dispatcher Start]
       │
       ▼
[Read GitHub Issues & PRs]
       │
       ├─► status:in-progress Issues -> Treated as running
       ├─► status:blocked / status:queued -> Re-evaluated
       └─► Open PR branches -> Progress state reconstructed
       │
       ▼
[Reconstruct DAG State & Resume]
```

---

## 2. GitHub as the Source of Truth

* **GitHub as the Source of Truth**:
  By fetching active PR branches and GitHub Issue labels (`status:in-progress`, `status:blocked`, `status:queued`), Orchestune rebuilds the DAG state in memory and resumes the cycle seamlessly from where it left off.
* **Reclaim counts (#512)**:
  The zombie/timeout reclaim counts (the `task_reclaim_counts` ledger behind `--max-task-reclaims`) live only in `run_state.json`, so losing that file resets them to zero. A task that already exceeded the limit stays stopped even so, because its `status:blocked-human-review` label on GitHub is the source of truth — only tasks still below the limit start their count over.

---

## 3. Repository Consistency Control Loop

State recovery is complemented by a repository-wide consistency kernel. Observers normalize facts from GitHub, Git, worktrees, processes, external executions, and `run_state.json` into an immutable `ObservedRepositoryState`. A pure derivation builds `DesiredRepositoryState` from task lifecycle, dependencies, dispatch policy, and the pending `TransitionIntent` journal. Pure invariants compare the two models and emit stable, evidence-bearing findings; planners may translate only known, automatic findings into typed `RepairCommand` values. `ConsistencySupervisor` is the only owner of repair decisions, ordering, bounded retries, authoritative re-observation, and result aggregation. Typed executors route commands to the existing low-level Forge, filesystem, process, and state-file operations only after their live preconditions have been revalidated.

The supervisor runs an authoritative full scan at cycle start and end, with targeted scans for process-local `StateChanged` events. The end scan therefore catches out-of-process changes that emitted no event. Modes are deliberately staged:

| Mode | Semantics |
|---|---|
| `off` | Do not run the additional repository-wide start/end control loop. The built-in safe Supervisor repair boundaries remain enabled for backward compatibility. |
| `shadow` | Run the additional repository-wide observe/derive/evaluate/plan loop without adding mutations. Built-in safe repairs still follow `--apply` as they do in `off`. |
| `repair` | In addition to the built-in safe repairs, execute finding or command codes explicitly named in the user repair allowlist. An empty user allowlist leaves the additional loop report-only. |

The backward-compatible built-in allowlist consists of the status findings `status.blocked-with-resolved-dependencies` and `status.primary-status-conflict`, plus the typed execution commands `execution.requeue`, `execution.update-bookkeeping`, and `execution.reclaim`. It is intentionally separate from `--consistency-repair-code`: an empty or limited user allowlist cannot disable these established repairs. Only codes that reached a built-in repair pass are removed from the later optional loop, so an attempted command is not retried in the same cycle while unattempted planner candidates remain eligible for explicit opt-in.

With `--apply`, built-in boundaries may mutate and `repair` mode may also execute user-allowlisted codes. With `--no-apply`, no external or durable repair side effect is made: candidates are reported as deferred, GC events are previews, and recovery bookkeeping may update only the ephemeral in-memory preview used by that cycle. This gives the migration path `off` (established behavior) → `shadow` (inspect the additional reports) → `repair` with an empty allowlist (same mutations, explicit repair outcomes) → `repair` with a limited allowlist.

Repair mode executes at most the configured number of passes (1–5). Each pass rechecks live preconditions, records an Intent before a non-atomic status transition, executes an idempotent command key at most once per cycle, and performs a fresh full observation afterward. Unknown or stale observations, ambiguous ownership, manual/non-repairable findings, and non-allowlisted findings remain report-only. A command whose typed handler is unexpectedly absent fails closed; it is never delegated through a phase-owned `SKIPPED` fallback. Boundary and final-loop reports are merged into the final cycle JSON and `events.jsonl`, which distinguish `resolved`, `unresolved`, `deferred`, `failed`, and `observation-unknown`. A failed attempt remains visible after aggregation, and a failed authoritative re-observation is `observation-unknown`, not `resolved`. Every pass also includes command status and diagnostics. New observers, invariants, planners, or executors extend their Protocol boundary rather than adding callbacks to the immutable state models.
