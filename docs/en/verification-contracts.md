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
| `P3C-LIVENESS-BOUND` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `event-only`, `empty-completion-set`, `throwaway-context` | - |
| `P3C-INTERMEDIATE-LIVENESS` | 3c | verified | `intermediate-ignored`, `promotion-suppressed` | - |
| `P3C-SAFETY` | 3c | verified | `hold-guard-bypass`, `reservation-guard-bypass`, `stale-evidence`, `action-mapping-dropped` | - |
| `P3C-INTERMEDIATE-SAFETY` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-QUINT-MODEL` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `stale-evidence`, `intermediate-ignored`, `reservation-guard-bypass`, `hold-guard-bypass`, `event-only` | - |
| `P3C-QUINT-OBSERVATION` | 3c | verified | `action-mapping-swapped` | - |
| `P3C-DRYRUN-DEPENDENCY-RESERVATION` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-DRYRUN-RESERVATION` | 3c | known_defect | - | #1281, #1283 |
| `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` | 3c | known_defect | - | #1281 |

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
| `P3C-QUINT-MODEL` | the Quint model keeps safety, bounded liveness (N = 1), event = real label (apply) and a read-only dry run | finite topologies (up to four dependency Issues), the model's fairness, sampled search with the recorded seed and bounds | model state after each action | no invariant violation; each model fault violates exactly its invariants |
| `P3C-QUINT-OBSERVATION` | at every replayed step the production state agrees with the model before the action runs | the action table of `tests/quint_replay.py` | each transition of a replayed ITF trace | every precondition holds on the real harness |
| `P3C-DRYRUN-DEPENDENCY-RESERVATION` | a dry-run preview of T or an intermediate node respects an unreleased completion reservation of the node's dependency | the preview is defined over the context snapshot (#1267 fixed this side) | dry-run cycle, checked by a deterministic scenario (the machine excuses T's dry-run reservation previews) | no preview while D's reservation is unreleased |
| `P3C-DRYRUN-RESERVATION` | a dry-run preview of T or an intermediate node respects the node's own unreleased reservation | the preview is defined over the context snapshot | dry-run cycle | no preview under T's own or an intermediate node's own reservation |
| `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` | after Forge-faulted apply cycles a dry run still previews T whose dependency has only prior-merge evidence | fair cycle (promotable at both boundaries, no injected failure) | the dry-run cycle after two faulted apply cycles | T appears in the preview (N = 1) |

### Rows that are not `verified`

- `P1-ESCALATION-NOT-ACTIVE` (unverified): a property of the test's rule choice, not of production code; no fault can be injected
- `P1-EXTERNAL-CHANGE` (out_of_scope): documented non-guarantee (status-labels.md); recovery is Phase 2
- `P2-RECOVERY-STABLE` (unverified): no control: a repair that oscillates after convergence has not been injected
- `P2-CONVERGENCE-UPPER-BOUND` (unverified): no test drives more than one pass; only the expectation (1 cycle) is checked
- `P3A-DOCUMENT-TABLE` (unverified): no control: a document drift has not been injected
- `P3B-LAUNCH-AND-ESCALATION` (unverified): plain assertions without contract ids; no control was injected
- `P3B-PERSISTENT-BUDGET-PRESERVED` (known_defect): strict xfail pins the counterexamples; the fix removes the marks
- `P3C-DRYRUN-RESERVATION` (known_defect): strict xfail counterexamples (#1283 for T, #1281 counterexample 1 for an intermediate node); assert_safe and the intermediate check excuse these previews until the production fixes land; the dependency side is a separate verified contract
- `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` (known_defect): #1281 counterexample 2: a strict xfail pins it; whether production drops the prior-merge completion set or the fairness judgement is wrong is not yet separated

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
| `reservation-guard-bypass` | the reservation checks of the dependency side, the target side or the intermediate's dependency always pass | `P3C-SAFETY`, `P3C-INTERMEDIATE-SAFETY` (machine, apply cycles), `P3C-DRYRUN-DEPENDENCY-RESERVATION` (deterministic dry run) | apply cycles in the machine; the dependency side of a dry run in a deterministic scenario |
| `stale-evidence` | a reopened dependency is still treated as completed | `P3C-SAFETY` | revoked label evidence |

Known gaps, recorded and not weakened: the random machine's `assert_safe` excuses every dry-run preview under an unreleased reservation, so it detects `reservation-guard-bypass` only in apply cycles (the intermediate check excuses only a node's own reservation). The dependency side of a dry run (fixed in #1267), for T and for an intermediate node, is therefore checked by its own deterministic scenario (`P3C-DRYRUN-DEPENDENCY-RESERVATION`); the previews under T's own reservation and an intermediate node's own reservation stay a known defect until #1281 and #1283 are fixed (`P3C-DRYRUN-RESERVATION`). Two guards (`fresh-guard-bypass` with `status_repair_preserves_protection` alone, and a premature intermediate assessment) are re-validated by a second layer and cannot be bypassed with one fault; the controls inject the whole guard.

<a id="quint-model"></a>
<!-- quint-model -->
## Quint model and production replay

`specs/quint/dependency_liveness.qnt` models the bounded liveness of dependency resolution (#1276). It is **a sampled exploration by the Quint simulator (`quint run`), not a proof**: nothing is claimed about arbitrary graphs, unbounded disturbance sequences or states the sampled sequences did not reach. `quint verify` (Apalache, JVM) is neither required nor used.

**Three layers; none replaces another.**

1. *Model*: the fixed-seed exploration and every saved scenario must keep the model's own invariants (`P3C-QUINT-MODEL`): `safety` (nothing is promoted against an obligation), `liveness` (every fair cycle ends with the promotion, N = 1; apply needs the real label, a dry run this cycle's preview), `eventMatchesLabel` (an apply cycle's events are its real label changes) and `dryRunReadOnly`.
2. *Replay*: every transition of the generated and of the saved ITF traces is executed on the production harness (`LivenessWorld`: the real `_prepare_cycle_context` and `execute_pipeline`). After each cycle the production observation (labels, `PromotionEvent` previews) is compared with the model's obligations; before each action the production state is compared with the model's (`P3C-QUINT-OBSERVATION`). Expectations come from the model state only; production is never asked whether a dependency is complete.
3. *Controls*: one model fault each (the fault scenarios run on the correct model and must pass, and on the faulty model must violate exactly the expected invariants at the planned step) and one production fault each (the same trace replayed with the fault must violate the expected contract). A parse, type or runtime error and a timeout are tool errors, never a detection.

**Toolchain.** Node.js (the major version in `package.json` `engines`; the CI uses `actions/setup-node`) is a required dependency of the local CI. `scripts/quint-check.sh` / `scripts/quint-check.ps1` run `npm ci` and verify the pinned `@informalsystems/quint` (exact version in `package.json` and `package-lock.json`); a missing Node.js stops the local CI with exit 2 and the install steps, never a skip. The simulator runs with `--backend=typescript` (it ships inside the pinned package; the default `rust` backend downloads an unpinned binary at first use).

**Exploration and its record.** Every local CI run explores with the same seed and bounds (`EXPLORATION` in `tests/quint_replay.py`: seed `0x2f9c`, at most 1500 samples of at most 30 steps, 100 traces written with `--mbt --out-itf --n-traces`) into `.orchestune/tmp/quint-replay-1276-…/` (created by `scripts/create-session-dir.*`, reused in the same run), replays all traces and requires a positive number of traces, transitions, cycles and checked obligations. `exploration-summary.json` in that directory records the tool versions, seed, bounds, duration and counts, and the local CI prints it. An empty or truncated output, a broken ITF, an unsupported action or value, or a trace with only an initial state fails the run.

**What the model states.** A cycle's state records `mustNot` (nodes that must not be promoted) and `mustQueue` (nodes that must be queued or previewed). Evidence is three-valued (`yes` / `no` / `maybe`): where the model cannot know the outcome (an injected Forge failure can abort a cycle at any mutation; ledger evidence collected under listing lag, a reservation or a failure; evidence after a ledger loss) it states **no obligation** instead of guessing. Not modeled, hence not claimed: an apply cycle with an armed failure *and* a pending stale change (the failure can abort the cycle before the change); holds on intermediate nodes (the harness has no such path); more than four dependency Issues or a chain deeper than two. A fault armed for an apply cycle makes that cycle unfair in the model even if no mutation hit it, which is weaker than the Hypothesis machine, not stronger.

**Action table** (the `ACTIONS` table in `tests/quint_replay.py`; an action or choice outside it fails the replay):

| Model action | Choices (`mbt::nondetPicks`) | Harness operation | Boundary |
|---|---|---|---|
| `init` / `init_<scenario>` | topology and `recompute` (read from the state) | `LivenessWorld(topology=…)` built from the state's `shape`; not run again as an action | initial labels of every Issue are compared |
| `complete_dependency` | `path` (`label`, `record_completion`, `outcome_not_needed`, `prior_merge`), `dep` (11-14) | `LivenessWorld.complete(dep, path)` | precondition: `dep` is queued and has no evidence |
| `duplicate_completion` | `dep` | `LivenessWorld.duplicate(dep)` | precondition: `dep` has evidence |
| `cycle` | `apply`, `lag` | one real cycle with `listing_lag = lag`; the pending stale change runs as `before_promotion` | `mustNot` / `mustQueue` against labels and `PromotionEvent` |
| `restart` | `ledgerLoss` | `lose_ledger()` when true (run-state and intent journal: reservations and uncollected ledger evidence) | - |
| `fail_next` | `op` (`add`, `remove`), `mode` (`before`, `after`) | `FaultPlan.forge_operation` | consumed by the next cycle |
| `toggle_hold` | `kind` (`base_red`, `recompute`, `reservation`), `target` | label on T, or `set_reservation(target)` on T, a dependency or an intermediate node; the new value comes from the next model state | - |
| `stale_snapshot` | `kind` (`add_hold`, `revoke`, `complete`), `target` | the change between context build and promotion of the next cycle | run only if the model says it applied it (`staleApplied`) |

**Saved scenarios.** The scripted scenarios of the model (`init_<id>`; one fixed choice per step, so the ITF has the same shape as a random trace) cover final and intermediate nodes, apply and dry run, live and lagged observation, no-op and real changes, dependency-side and target-side holds, failures, restarts, duplicates and stale changes. `tests/fixtures/quint/dependency_liveness_traces.json` stores the ITF with its seed, bounds, Quint version and generation command; a test regenerates each and fails when the model changed without `uv run python -m tests.quint_replay regenerate`. `tests/fixtures/quint/dependency_liveness_fault_scenarios.json` stores the model-fault scenarios with the expected invariants and planned step.

**Known production counterexamples (#1281, #1283).** The model states their obligations; production violates them. The exploration runs with the model's `guard` set so that random traces never assert them (the Hypothesis machine excuses the same cases). The three saved `defect_*` scenarios run with the guard off and are replayed by strict xfail tests that register the Issue, the contract, the step and the node: only that mismatch is the defect; any other mismatch, in the same trace or elsewhere, fails.

| Fault | Model: violated invariant (scenario) | Replay: violated contract (stored scenario) |
|---|---|---|
| `promotion-suppressed` | `liveness` (`final_apply`) | `P3C-LIVENESS-BOUND` (`final_apply`) |
| `promotion-delayed` | `liveness` (`final_apply`) | `P3C-LIVENESS-BOUND` (`final_apply`) |
| `stale-evidence` | `safety` (`stale_revoke`) | `P3C-SAFETY` (`stale_revoke`) |
| `intermediate-ignored` | `liveness` (`chain_apply`) | `P3C-INTERMEDIATE-LIVENESS` (`chain_apply`) |
| `reservation-guard-bypass` | `safety` (`dependency_reservation_apply`) | `P3C-SAFETY` (`dependency_reservation_apply`) |
| `hold-guard-bypass` | `safety` (`base_red_apply`) | `P3C-SAFETY` (`base_red_apply`) |
| `event-only` | `eventMatchesLabel`, `liveness` (`final_apply`) | `P3C-LIVENESS-BOUND` (`final_apply`) |
| `empty-completion-set` | - | `P3C-LIVENESS-BOUND` (`recompute_release`) |
| `throwaway-context` | - | `P3C-LIVENESS-BOUND` (`lagged_apply`) |
| `action-mapping-swapped` | - | `P3C-QUINT-OBSERVATION` (`duplicate_completion`) |
| `action-mapping-dropped` | - | `P3C-SAFETY` (`restart_loses_ledger`) |

## Guarantee and limits

Verified means the tests above fail for the injected faults listed, in the deterministic scenarios described. It does not mean that no other fault exists, that arbitrary graphs or unbounded disturbance sequences are covered, or that the random exploration reaches every state. Production defects found while auditing are split into their own Issues with a counterexample and pinned with a strict xfail; expectations, fairness and bounds are not weakened to make an audit pass.

## Reproduction

```bash
uv run pytest tests/test_verification_contracts.py tests/test_status_machine_stateful.py tests/test_status_reconciliation_stateful.py tests/test_consistency_status_repairs.py tests/test_status_events.py tests/test_status_events_stateful.py tests/test_status_event_retry_resume.py tests/test_dependency_liveness_stateful.py -n0 --hypothesis-profile ci
```

Replay a random failure with `--hypothesis-seed=<seed>`; the control scenarios need no seed.

The Quint model and its replay (Node.js is required; `scripts/quint-check.sh` installs the pinned tools):

```bash
./scripts/quint-check.sh                       # Windows: .\scripts\quint-check.ps1
uv run pytest tests/test_quint_dependency_replay.py -n0
uv run python -m tests.quint_replay regenerate   # after changing the model
```

To rerun one scenario by hand, use the command stored beside its trace in `tests/fixtures/quint/dependency_liveness_traces.json` (`node_modules/.bin/quint run specs/quint/dependency_liveness.qnt --backend=typescript --init=init_<id> …`); the exploration command, seed and bounds are in `exploration-summary.json`.
