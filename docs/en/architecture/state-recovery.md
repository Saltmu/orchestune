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

State recovery is complemented by a repository-wide consistency kernel. Observers normalize facts from GitHub, Git, worktrees, processes, external executions, and `run_state.json` into an immutable `ObservedRepositoryState`. A pure derivation builds `DesiredRepositoryState` from task lifecycle, dependencies, dispatch policy, and the pending `TransitionIntent` journal. Pure invariants compare the two models and emit stable, evidence-bearing findings; planners may translate only known, automatic findings into typed `RepairCommand` values. Forge, filesystem, process, and state-file mutations remain behind explicit executors at the existing dispatch phase boundaries.

The supervisor runs an authoritative full scan at cycle start and end, with targeted scans for process-local `StateChanged` events. The end scan therefore catches out-of-process changes that emitted no event. Modes are deliberately staged:

| Mode | Semantics |
|---|---|
| `off` | No consistency scans; the established self-healing phases retain their existing default behavior. |
| `shadow` | Observe, derive, evaluate, and plan, but never execute a repair. |
| `repair` | Execute only finding or command codes explicitly named in the repair allowlist. An empty allowlist is report-only, so newly added policies remain shadow-only until deliberately enabled. |

Repair mode executes at most the configured number of passes (1–5). Each pass rechecks live preconditions, records an Intent before a non-atomic status transition, executes an idempotent command key at most once per cycle, and performs a fresh full observation afterward. Unknown or stale observations, ambiguous ownership, manual/non-repairable findings, unsupported phase-owned commands, and non-allowlisted findings remain report-only. The final cycle JSON and `events.jsonl` distinguish `resolved`, `unresolved`, `deferred`, `failed`, and `observation-unknown`; every pass also includes command status and diagnostics. New observers, invariants, planners, or executors extend their Protocol boundary rather than adding callbacks to the immutable state models.
