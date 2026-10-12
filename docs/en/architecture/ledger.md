# Shared Execution Ledger & Package Boundaries

This document describes the responsibilities and dependency rule of `ledger`, the L2 shared infrastructure for `claim`, `complete`, `dispatch`, `integrator`, and status reporting, together with the ownership contracts and the package boundaries of `targets`, repository configuration, and the consistency status kernel. For the module-layer overview, see [Architecture & Design §4](../architecture.md#4-module-layers--package-boundary).

---

## 1. Ledger modules and their responsibilities

- `ledger.completion_reservations`: shared conservative guards that keep claim, GC, repair, and dependency readers from mutating or releasing an Issue while its completion proof is pending.
- `ledger.run_state`: execution-state models, loading, validation, normalization,
  persistence, and retention helpers. Moving it out of dispatch preserves the
  JSON format and the requirement to hold the existing state lock when saving.
- `ledger.active_records`: owns `ActiveWorktree` and its immutable structure. `ActiveWorktree` is the only in-memory source of truth, made of `core` plus three frozen subrecords (`launch`, `claim`, `completion`).
  - Historical flat attribute properties have been eliminated, and `slots=True` rejects stale writes or accesses.
  - `from_records()` is the typed nested construction path, while `with_core()` updates the shared fields; records are not stored twice.
  - Owner fields are replaced only through their owner APIs: claim fields through `claim.ownership` (`build_claim_info()`, `with_claim()`), launch fields through `dispatch.launch_state` (`build_launch_record()`, `with_launch()`, `with_launch_phase()`), and completion fields through `complete.journal` (`active_completion_from_record()`, `with_completion()`).
  - For detailed specifications and lifecycle priority rules, see [Stateless CI & Self-Healing State Recovery (state-recovery.md)](state-recovery.md#active-worktree-lifecycle).
- `ledger.active_codec`: explicit decoding and encoding of the established flat
  ActiveWorktree JSON shape. It preserves the field order and omission of a null
  `completion_policy_config` while keeping codec details out of state validation.
- `ledger.active_lifecycle`: `lifecycle(active)` maps persisted fields to
  a candidate phase in completion, launch, recovery sentinel, claim, reservation
  order. `HANDOFF_READY` is only a candidate; existing code still verifies
  completion receipts and remote evidence. `ledger.run_state` re-exports the
  lifecycle enum and helper.

  | Candidate phase | Condition, checked in order |
  | --- | --- |
  | `HANDOFF_READY` | Handoff-ready flag or a handoff completion stage is present. |
  | `COMPLETING` | Completion ID is present. |
  | `RUNNING` | PID, external handle, or launched phase is present. |
  | `LAUNCHING` | Start time, launch attempt ID, or another launch phase is present. |
  | `RECOVERY_REQUIRED` | Recovered claim ID or unverifiable-owner digest sentinel is present. |
  | `CLAIMED` | Interactive owner with a non-null claim stage other than `reserved`. |
  | `RESERVED` | Fallback, including an initial `reserved` claim or an in-memory record with no claim stage. Persisted records require this field. |
- `ledger.status_labels`: shared status-label constants and the application of
  a plan: add, the ledger-commit callback, then sequential removal of old labels.
- `ledger.status_machine`: a pure, Forge-independent module holding the
  `status:*` role table, the normal lifecycle transition table, and the plan
  derived from a label snapshot. It reads or writes no labels, runs no
  callbacks, and rejects no transition in production.
- `ledger.status_events`: the `status:*` Event model (#1219 Phase 3). A pure
  module that decides events, execution identity, retry reservations and
  budgets without side effects; it depends only on `status_machine`, `labels`
  and `bounded_limit`. It replaces no production decision; conformance tests
  tie it to the production functions. The backoff retry decision is owned by
  `dispatch.retry_policy`, so callers inject it as `BudgetLimits.plan_backoff`
  instead of ledger importing dispatch.
- `ledger.escalation`: the shared escalation operation. It adds the terminal
  label, invokes the caller's state-commit callback, removes the specified old
  labels, then posts the reason. A failed add or callback stops later steps;
  later cleanup/comment failure does not roll back a committed state update.
  Dispatch-specific rule evaluation, process stopping, and ledger updates stay
  with the dispatch workflow and its callbacks.

## 2. Ownership contracts

`ownership_contracts` is an independent L0 module defining `OwnerKind`,
`ClaimStage`, and `ReservationKind`. Ledger imports it directly; `claim.contracts`
may re-export those same enums and retains claim-specific requests/outcomes.
The shared enums have no dependency on claim, ledger, or dispatch implementations.

## 3. The ledger dependency rule

The dedicated ledger rule is stricter than the general layer rule: ledger
may import its own modules, but its other dependencies inside `orchestune` must
be L0/L1. Standard-library and third-party imports are outside this layer check.

- Ledger does not depend on claim or dispatch.
- `complete` no longer imports dispatch; the former `dispatch.state` and `dispatch.labels` modules have been removed.
- Integrator imports shared escalation from ledger and `file_lock` directly from `infra.process_utils`.

For what CI checks, see rule 6 in [Architecture & Design §4.2](../architecture.md#42-invariants-enforced-by-ci).

## 4. Shared package boundaries

### 4.1 targets

The coordinator uses shared routine constants and implementation from `targets.cloud_routine`, and execution contracts from `targets.contracts`. Shared launch instructions, branch verification, and completion evidence live in `targets.support`. Dispatch re-exports the same objects for compatibility; the shared targets package does not import dispatch or integrator.

### 4.2 Where shared types and decisions live

Types and decisions used by several workflows live in shared modules rather than in a workflow package, so `claim` and `integrator` need no dependency on `dispatch`.

| What | Where |
| --- | --- |
| Shared lock types and constants (`KIND_BRANCH`/`KIND_PR`/`ExternalLockConflict`/`ExternalLockScanResult`) | the L0 `lock_contracts` module |
| Dependency resolution, assessment, policy, and legacy resolution | the shared L2 `dependencies` package (`dependencies.resolution`, `dependencies.assessment`, `dependencies.policy`) |
| Plan identity | the L0 `plan_identity` module |
| Symbol verification | `dag.symbol_verification`, inside the `dag` package |

### 4.3 Repository configuration

Repository TOML discovery and repository-root lookup live in the L1
`infra.repository_config` adapter, and the `ConfigError` type lives in the L0
`config_contracts` module. The L2 `targets.completion_policy` interprets
effective token limits and resolves usage through the shared `targets.usage`
provider; completion policy does not import dispatch configuration or workflow
modules.

### 4.4 Consistency status kernel

The consistency status kernel derives lifecycle vocabulary from the status-machine role table, while preserving its seven-label tuple and the ledger's three ACTIVE labels.

- Its invariant/planner/fresh executor protect FINAL and ESCALATION labels, retain the exact done+queued rollback exception, and honor promotion holds for initialization and conflict repair.
- The executor persists and verifies Intents; its transition-table warning is observational only.
- Supervisor pass/cycle distinction and bounded convergence assumptions are documented in [status labels](../status-labels.md#status-reconciliation-safety-and-convergence-1218).

Policy choices in RuleChain, GC, rebase and Integrator remain separate.
