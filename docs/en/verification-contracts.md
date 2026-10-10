# Verification contracts and fault-detection controls

Each guarantee of Phases 1-3 is one row of `tests/verification_contracts.py`. A row names the guarantee, its premise, the observation boundary, the expectation, the tests that verify it on production code and the **controls** that prove those tests can fail: a control injects exactly one fault (in the test only, with `monkeypatch`; production code has no fault flags), runs the same deterministic scenario as the normal test, and succeeds only if the scenario violates **the expected contract id** (`ContractViolation`). A different contract, a failed precondition, an unrelated exception or no failure at all fails the control. Random exploration is never the evidence of detection. `tests/test_verification_contracts.py` checks the table against real pytest collection (parametrized cases included; skipped or xfailed tests do not count) and against the tables below.

<!-- contract-table -->
## Contract table

Statuses: `verified` = a normal test and at least one control; `unverified` = a guarantee exists but no control does (reason below); `known_defect` = a production defect pinned by a strict xfail or an explicit exclusion in a separate Issue; `out_of_scope` = a documented non-guarantee. The table says nothing stronger than its statuses: nothing here means "all guarantees are verified".

| ID | Phase | Status | Faults detected | Issues |
|---|---|---|---|---|
| `P1-LIFECYCLE-NONEMPTY` | 1 | verified | `remove-before-add` | - |
| `P1-SUCCESS-TARGET-ONLY` | 1 | verified | `remove-missing` | - |
| `P1-RETRY-CONVERGES` | 1 | verified | `retry-noop` | - |
| `P1-ESCALATION-NOT-ACTIVE` | 1 | unverified | - | - |
| `P1-EXTERNAL-CHANGE` | 1 | out_of_scope | - | - |
| `P2-FINAL-ESCALATION-PROTECTED` | 2 | verified | `fresh-guard-bypass` | - |
| `P2-PLAN-HUMAN-GATE` | 2 | verified | `planner-gate-bypass` | - |
| `P2-PROMOTION-HOLD` | 2 | verified | `hold-guard-bypass` | - |
| `P2-RECOVERY-ONE-CYCLE` | 2 | verified | `repair-disabled`, `repair-delayed` | - |
| `P2-RECOVERY-STABLE` | 2 | unverified | - | - |
| `P2-LIVE-VERIFICATION` | 2 | verified | `event-only` | - |
| `P2-CONVERGENCE-UPPER-BOUND` | 2 | unverified | - | - |
| `P3A-ROUTE-COVERAGE` | 3a | verified | `unrouted-source`, `unexecuted-route-condition` | - |
| `P3A-DYNAMIC-CONFORMANCE` | 3a | verified | `misrouted-event`, `wrong-model-target` | - |
| `P3A-DOCUMENT-TABLE` | 3a | unverified | - | - |
| `P3B-BUDGET-CONSUMED` | 3b | verified | `budget-not-consumed` | - |
| `P3B-RETRY-BOUND` | 3b | verified | `budget-bound-exceeded` | - |
| `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | 3b | verified | `persistent-budget-reset` | - |
| `P3B-LAUNCH-AND-ESCALATION` | 3b | unverified | - | - |
| `P3B-PERSISTENT-BUDGET-PRESERVED` | 3b | known_defect | - | #1279, #1280 |
| `P3C-CASE-DELAY` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `empty-completion-set`, `throwaway-context` | - |
| `P3C-LIVENESS-BOUND` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `event-only` | - |
| `P3C-INTERMEDIATE-LIVENESS` | 3c | verified | `intermediate-ignored`, `promotion-suppressed` | - |
| `P3C-SAFETY` | 3c | verified | `hold-guard-bypass`, `reservation-guard-bypass`, `stale-evidence` | - |
| `P3C-INTERMEDIATE-SAFETY` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-DRYRUN-RESERVATION` | 3c | known_defect | - | #1281, #1283 |

### Details

| ID | Guarantee | Premise | Boundary | Expectation |
|---|---|---|---|---|
| `P1-LIFECYCLE-NONEMPTY` | at least one lifecycle label after every Forge add/remove | no external change; a single unrecovered operation | each Forge operation (not only the adapter's return) | always, including right after an injected failure |
| `P1-SUCCESS-TARGET-ONLY` | a normal finish with a complete removal list leaves the target alone | complete old-label list; no failure | adapter return | lifecycle == {target}; auxiliary labels untouched |
| `P1-RETRY-CONVERGES` | one complete retry after a partial failure converges to the target | add, callback and every remove of the retry succeed | retry return; replay leaves labels unchanged | exactly one complete retry |
| `P1-ESCALATION-NOT-ACTIVE` | normal rules never choose an ESCALATION -> ACTIVE transition | rule choice in the test; the adapter does not refuse it | rule selection | never |
| `P1-EXTERNAL-CHANGE` | after an arbitrary external relabel | external_relabel may delete or add any lifecycle label | - | no unconditional guarantee; the model starts a new epoch |
| `P2-FINAL-ESCALATION-PROTECTED` | a stale plan never re-activates a FINAL or ESCALATION task | the label changes between planning and the executor's fresh read | executor fresh guard | labels stay exactly the protected one |
| `P2-PLAN-HUMAN-GATE` | the planner never plans the removal of a human gate | a report may claim anything | planner output | empty plan |
| `P2-PROMOTION-HOLD` | a promotion hold that appears before execution blocks the promotion | ci:base-branch-red / blocked-recompute added after planning | executor fresh guard | status:queued is never added while a hold is present |
| `P2-RECOVERY-ONE-CYCLE` | a repairable case converges in exactly one recovery cycle | complete KNOWN facts, stable evidence, apply, all commands allowed, no holds | end of the first cycle (expectation 1; upper bound k=3 is separate) | labels == target and no finding after cycle 1 |
| `P2-RECOVERY-STABLE` | two further cycles keep labels, plans, history and the Intent set | after P2-RECOVERY-ONE-CYCLE | cycles 2 and 3 | no change |
| `P2-LIVE-VERIFICATION` | an applied repair is backed by the live labels | the Forge accepted the command | repair result status | APPLIED only when the live primary status is the target |
| `P2-CONVERGENCE-UPPER-BOUND` | convergence within the upper bound k=3 | as P2-RECOVERY-ONE-CYCLE | cycle k | k=3 |
| `P3A-ROUTE-COVERAGE` | every production source has its routes and every route condition is executed | static table of call sites, out-of-scope paths and invariant paths | route table | complete |
| `P3A-DYNAMIC-CONFORMANCE` | the production driver reaches the labels and state the Event model predicts | executed case table; static coverage is a separate contract | each step: labels, result, completion, execution, retries, counts | equal to apply_event |
| `P3A-DOCUMENT-TABLE` | status-labels.md lists every route with its sources and targets | the document is the specification readers use | document table | equal to the route table |
| `P3B-BUDGET-CONSUMED` | each new logical retry consumes one budget slot; a resume consumes none | budgeted events with distinct operations | each delivery | count + 1 (resume or exhausted: unchanged) |
| `P3B-RETRY-BOUND` | new retries per budget never exceed the specification table within a ledger epoch | default limits; local budgets reset only on ledger loss | each counted retry | RETRY_BOUNDS |
| `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | ledger loss resets only the local budgets | recompute and base-branch-red live on the Forge | restart(ledger_loss=True) | persistent counts unchanged |
| `P3B-LAUNCH-AND-ESCALATION` | no duplicate active execution, no relaunch after completion, escalation is irreversible | Event model driven by random sequences | each delivery | invariants 3-5 |
| `P3B-PERSISTENT-BUDGET-PRESERVED` | a GC reclaim keeps the backoff budgets; a relaunch keeps the recompute budget | production persistence of TaskReclaimRecord and Issue-body counters | persisted records | budgets survive |
| `P3C-CASE-DELAY` | per completion path, T is promoted exactly at its case-table cycle (d = 0) | evidence usable for the promotion decision in cycle 0 | cycle of the evidence; never earlier, never later | d from the case table |
| `P3C-LIVENESS-BOUND` | T is queued (apply) or previewed (dry run) after N = 1 fair cycle | fair cycle: eligible at cycle start and at the promotion point, no error or injected failure | end of the first fair cycle; apply checks the real label, dry run this cycle's preview | N = 1 |
| `P3C-INTERMEDIATE-LIVENESS` | an intermediate node is promoted (apply) or previewed (dry run) after N = 1 fair cycle | its own dependencies are valid and unreserved at both boundaries | end of the first fair cycle for that node | N = 1 |
| `P3C-SAFETY` | no promotion (or dry-run preview) without valid evidence, or under a hold or reservation | apply is judged at the promotion point, a dry run at cycle start | every cycle | never |
| `P3C-INTERMEDIATE-SAFETY` | an intermediate node is not promoted before its own dependencies are valid and unreserved | apply is judged at the promotion point | every cycle | never |
| `P3C-DRYRUN-RESERVATION` | a dry-run preview respects unreleased completion reservations | the preview is defined over the context snapshot | dry-run cycle | no preview under a reservation of D, T or an intermediate node |

### Rows that are not `verified`

- `P1-ESCALATION-NOT-ACTIVE` (unverified): a property of the test's rule choice, not of production code; no fault can be injected
- `P1-EXTERNAL-CHANGE` (out_of_scope): documented non-guarantee (status-labels.md); recovery is Phase 2
- `P2-RECOVERY-STABLE` (unverified): no control: a repair that oscillates after convergence has not been injected
- `P2-CONVERGENCE-UPPER-BOUND` (unverified): no test drives more than one pass; only the expectation (1 cycle) is checked
- `P3A-DOCUMENT-TABLE` (unverified): no control: a document drift has not been injected
- `P3B-LAUNCH-AND-ESCALATION` (unverified): plain assertions without contract ids; no control was injected
- `P3B-PERSISTENT-BUDGET-PRESERVED` (known_defect): strict xfail pins the counterexamples; the fix removes the marks
- `P3C-DRYRUN-RESERVATION` (known_defect): assert_safe and the intermediate checks excuse these previews until the production fix lands

<a id="cycle-definitions"></a>
<!-- cycle-definitions -->
## Cycle definitions

- **cycle 0**: the cycle in which valid evidence first becomes available as an input of the promotion (or repair) decision. Evidence injected before the decision of a cycle makes that cycle 0; evidence injected after it makes the next cycle 0.
- **expected delay d** (deterministic case tables): the cycle, counted from cycle 0, whose decision promotes. Dependency resolution has d = 0 for every case with an observable. A case also fails when the promotion is earlier.
- **bound N** (random sequences): promotion by the end of the N-th *fair* cycle. Dependency resolution has N = 1. The earlier "N+1 cycles" wording of design #1219 §4 is the same guarantee restated with N = 1; #1219 is closed and not edited.
- **fair cycle**: the premise holds both at the start of the cycle and at the promotion decision, and there is no error or injected failure. A disturbance that changes nothing does not reset the count. Apply cycles are judged at the promotion point (the executor re-reads the Forge); dry runs and cycles under listing lag at the start of the cycle (the context snapshot).
- **observation**: an apply cycle requires the real `status:queued` label; a dry run, which never changes labels, requires this cycle's own `PromotionEvent` preview (or T already queued). An earlier preview never satisfies a later cycle.
- **Phase 2**: *expectation*: a repairable case converges in exactly one recovery cycle (`P2-RECOVERY-ONE-CYCLE`). *Bound*: k = 3 (`P2-CONVERGENCE-UPPER-BOUND`, unverified). They are separate contracts.

### Which test detects what (measured)

The Issue's reading was that a one-cycle promotion delay on a path whose evidence arrives in the same cycle (`record_completion`) is seen by the case table but not by the random machine. The control tests show otherwise for apply cycles: the evidence is already persisted in the ledger when the cycle starts, so the cycle is fair and both the case table and the machine detect the delay. They differ in what else they pin: the case table also fails on a promotion that is *earlier* than d, while the machine requires only that a fair cycle ends with the promotion (early promotion is the safety contract `P3C-SAFETY`). A dry run of `record_completion` has no observable at all (#882), so neither side can detect a delay there; `test_control_dry_run_record_completion_has_nothing_to_detect` records that gap instead of hiding it.

<!-- detection-matrix -->
## Detection matrix

| Fault | Injected in the test by | Violates | Detected by |
|---|---|---|---|
| `remove-missing` | `plan_transition` drops the first removal | `P1-SUCCESS-TARGET-ONLY` | adapter scenario |
| `remove-before-add` | the adapter removes before it adds | `P1-LIFECYCLE-NONEMPTY` | per-operation observer (the final labels are correct) |
| `retry-noop` | the adapter skips the removals when the target is already present | `P1-RETRY-CONVERGES` | `add-response-lost` position |
| `fresh-guard-bypass` | `_fresh_preconditions_hold` always passes | `P2-FINAL-ESCALATION-PROTECTED` | stale plan, `done` and `blocked-human-review` |
| `planner-gate-bypass` | the planner's protection checks always pass | `P2-PLAN-HUMAN-GATE` | planner scenario |
| `hold-guard-bypass` | `PROMOTION_HOLD_LABELS` is empty in the executor | `P2-PROMOTION-HOLD` | stale plan with a late hold |
| `repair-disabled` / `repair-delayed` | the planner returns nothing always / in the first call | `P2-RECOVERY-ONE-CYCLE` | blocked with resolved dependencies |
| `event-only` | the command changes nothing, verification claims success | `P2-LIVE-VERIFICATION` (Phase 2), `P3C-LIVENESS-BOUND` (3c) | result status vs live labels; apply cycle label |
| `unrouted-source` / `unexecuted-route-condition` | a source is removed / a condition is never executed | `P3A-ROUTE-COVERAGE` | static check |
| `misrouted-event` / `wrong-model-target` | the route names another Event / the model returns another target | `P3A-DYNAMIC-CONFORMANCE` | production driver vs model |
| `budget-not-consumed` | `apply_event` keeps the previous retries | `P3B-BUDGET-CONSUMED` | reclaim loop |
| `budget-bound-exceeded` | the reclaim limit is raised above the table | `P3B-RETRY-BOUND` | reclaim loop |
| `persistent-budget-reset` | `restart` also zeroes the persistent counts | `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | ledger-loss scenario |
| `promotion-suppressed` / `promotion-delayed` | the promotion boundary is skipped always / in the evidence cycle | `P3C-CASE-DELAY` (case table), `P3C-LIVENESS-BOUND`, `P3C-INTERMEDIATE-LIVENESS` | label, `record_completion` (apply), prior merge (apply and dry run) |
| `empty-completion-set` / `throwaway-context` | the #902 Round 4/5 miswiring | `P3C-CASE-DELAY` | case table |
| `intermediate-ignored` | the intermediate node's assessment is dropped | `P3C-INTERMEDIATE-LIVENESS` | intermediate topology |
| `hold-guard-bypass` (3c) | every `PROMOTION_HOLD_LABELS` is empty | `P3C-SAFETY` | `ci:base-branch-red` before the cycle |
| `reservation-guard-bypass` | the reservation checks of the dependency side, the target side or the intermediate's dependency always pass | `P3C-SAFETY`, `P3C-INTERMEDIATE-SAFETY` | apply cycles only |
| `stale-evidence` | a reopened dependency is still treated as completed | `P3C-SAFETY` | revoked label evidence |

Known gaps, recorded and not weakened: dry-run previews under an unreleased reservation are excused in `assert_safe` and the intermediate checks until #1281 and #1283 are fixed (`P3C-DRYRUN-RESERVATION`), so `reservation-guard-bypass` is only detected in apply cycles. Two guards (`fresh-guard-bypass` with `status_repair_preserves_protection` alone, and a premature intermediate assessment) are re-validated by a second layer and cannot be bypassed with one fault; the controls inject the whole guard.

## Guarantee and limits

Verified means the tests above fail for the injected faults listed, in the deterministic scenarios described. It does not mean that no other fault exists, that arbitrary graphs or unbounded disturbance sequences are covered, or that the random exploration reaches every state. Production defects found while auditing are split into their own Issues with a counterexample and pinned with a strict xfail; expectations, fairness and bounds are not weakened to make an audit pass.

## Reproduction

```bash
uv run pytest tests/test_verification_contracts.py tests/test_status_machine_stateful.py tests/test_status_reconciliation_stateful.py tests/test_consistency_status_repairs.py tests/test_status_events.py tests/test_status_events_stateful.py tests/test_status_event_retry_resume.py tests/test_dependency_liveness_stateful.py -n0 --hypothesis-profile ci
```

Replay a random failure with `--hypothesis-seed=<seed>`; the control scenarios need no seed.
