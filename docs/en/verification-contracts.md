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
| `P3A-LAUNCH-IDENTITY-CONFORMANCE` | 3a | verified | `active-fact-guard-bypass` | #1286 |
| `P3A-DOCUMENT-TABLE` | 3a | unverified | - | - |
| `P3B-BUDGET-CONSUMED` | 3b | verified | `budget-not-consumed` | - |
| `P3B-RETRY-BOUND` | 3b | verified | `budget-bound-exceeded` | - |
| `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | 3b | verified | `persistent-budget-reset` | - |
| `P3B-LAUNCH-AND-ESCALATION` | 3b | unverified | - | - |
| `P3B-RECLAIM-BUDGET-PRESERVED` | 3b | verified | `reclaim-record-replaced` | - |
| `P3B-RECOMPUTE-BUDGET-PRESERVED` | 3b | verified | `relaunch-counter-reset` | - |
| `P3C-CASE-DELAY` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `empty-completion-set`, `throwaway-context` | - |
| `P3C-LIVENESS-BOUND` | 3c | verified | `ledger-premise-removed`, `prior-merge-repair-exclusion-removed`, `promotion-suppressed`, `promotion-delayed`, `event-only`, `empty-completion-set`, `throwaway-context` | - |
| `P3C-INTERMEDIATE-LIVENESS` | 3c | verified | `intermediate-ignored`, `promotion-suppressed` | - |
| `P3C-SAFETY` | 3c | verified | `hold-guard-bypass`, `reservation-guard-bypass`, `stale-evidence`, `action-mapping-dropped`, `event-only` | - |
| `P3C-INTERMEDIATE-SAFETY` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-QUINT-MODEL` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `stale-evidence`, `intermediate-ignored`, `reservation-guard-bypass`, `hold-guard-bypass`, `event-only`, `dry-run-writes` | - |
| `P3C-DRYRUN-READONLY` | 3c | verified | `dry-run-writes` | - |
| `P3C-QUINT-OBSERVATION` | 3c | verified | `action-mapping-swapped` | - |
| `P3C-DRYRUN-DEPENDENCY-RESERVATION` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-DRYRUN-RESERVATION` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` | 3c | verified | `prior-merge-repair-exclusion-removed` | - |
| `P3C-PROMOTION-REPORTED` | 3c | verified | `report-record-dropped`, `report-delivered-twice` | - |

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
| `P3A-ROUTE-COVERAGE` | every production source has its routes and every route condition is executed | syntax scan of production call sites -> CALL_SITES counts and set -> EVENT_BY_SOURCE routes and executed cases | call site registry and route table | complete |
| `P3A-DYNAMIC-CONFORMANCE` | the production driver reaches the labels and state the Event model predicts | executed case table; static coverage is a separate contract | each step: labels, result, completion, execution, retries, counts | equal to apply_event |
| `P3A-LAUNCH-IDENTITY-CONFORMANCE` | `CycleContext.record_launch` and the Event model agree on launch identity outcomes, and every source/condition protection scope is recorded | independent expected cases and a protection row for each `EVENT_BY_SOURCE` condition | RecordStatus/reason, effective labels, execution activity, fact, and no mutation on rejection | same active fact is NOOP, conflicting active fact is CONFLICT, confirmed inactive fact can launch again |
| `P3A-DOCUMENT-TABLE` | status-labels.md lists every route with its sources and targets | the document is the specification readers use | document table | equal to the route table |
| `P3B-BUDGET-CONSUMED` | each new logical retry consumes one budget slot; a resume consumes none | budgeted events with distinct operations | each delivery | count + 1 (resume or exhausted: unchanged) |
| `P3B-RETRY-BOUND` | new retries per budget never exceed the specification table within a ledger epoch | default limits; local budgets reset only on ledger loss | each counted retry | RETRY_BOUNDS |
| `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | ledger loss resets only the local budgets | recompute and base-branch-red live on the Forge | restart(ledger_loss=True) | persistent counts unchanged |
| `P3B-LAUNCH-AND-ESCALATION` | no duplicate active execution, no relaunch after completion, escalation is irreversible | Event model driven by random sequences | each delivery | invariants 3-5 |
| `P3B-RECLAIM-BUDGET-PRESERVED` | a GC reclaim reservation or dirty hold keeps the early-death, review-timeout and usage-limit budgets | production persistence of TaskReclaimRecord by the zombie, cloud and dirty-hold writers | persisted records | budgets survive |
| `P3B-RECOMPUTE-BUDGET-PRESERVED` | a relaunch keeps the recompute budget | production persistence of the Issue-body counters | persisted records | budgets survive |
| `P3C-CASE-DELAY` | per completion path, T is promoted exactly at its case-table cycle (d = 0) | evidence usable for the promotion decision in cycle 0 | cycle of the evidence; never earlier, never later | d from the case table |
| `P3C-LIVENESS-BOUND` | T is queued (apply) or previewed (dry run) after N = 1 fair cycle | fair cycle: eligible at cycle start and at the promotion point, no error or injected failure; a blocked-recompute T additionally needs an empty active ledger at the promotion point (#1281: production fails closed on an entry whose footprint deviation cannot be evaluated) | end of the first fair cycle; apply checks the real label, dry run this cycle's preview | N = 1 |
| `P3C-INTERMEDIATE-LIVENESS` | an intermediate node is promoted (apply) or previewed (dry run) after N = 1 fair cycle | its own dependencies are valid and unreserved at both boundaries | end of the first fair cycle for that node | N = 1 |
| `P3C-SAFETY` | no promotion (or dry-run preview) without valid evidence, or under a hold or reservation | apply is judged at the promotion point, a dry run at cycle start | every cycle | never |
| `P3C-INTERMEDIATE-SAFETY` | an intermediate node is not promoted before its own dependencies are valid and unreserved | apply is judged at the promotion point | every cycle | never |
| `P3C-QUINT-MODEL` | the Quint model keeps safety, bounded liveness (N = 1), every apply event is a real label change and a read-only dry run | finite topologies (up to four dependency Issues), the model's fairness, sampled search with the recorded seed and bounds | model state after each action | no invariant violation; each model fault violates exactly its invariants |
| `P3C-DRYRUN-READONLY` | a dry-run cycle changes no lifecycle label of T, an intermediate node or a dependency Issue | a dry run never mutates the Forge; hold labels and the fixture's own stale change are not the cycle's | every dry-run cycle of a replayed trace | labels unchanged |
| `P3C-QUINT-OBSERVATION` | at every replayed step the production state agrees with the model before the action runs | the action table of `tests/quint_replay.py` | each transition of a replayed ITF trace | every precondition holds on the real harness |
| `P3C-DRYRUN-DEPENDENCY-RESERVATION` | a dry-run preview of T or an intermediate node respects an unreleased completion reservation of the node's dependency | the preview is defined over the context snapshot (#1267 fixed this side) | dry-run cycle, checked by a deterministic scenario (the machine excuses T's dry-run reservation previews) | no preview while D's reservation is unreleased |
| `P3C-DRYRUN-RESERVATION` | a dry-run preview of T or an intermediate node respects the node's own unreleased reservation | the preview is defined over the context snapshot | dry-run cycle | no preview under T's own or an intermediate node's own reservation |
| `P3C-PROMOTION-REPORTED` | a status-repair promotion that production applied is reported exactly once in the confirmed event log: at once when its live verification holds, otherwise by the next undisturbed apply cycle | the status repair path (blocked -> queued for resolved dependencies) with a persisted unreported record, a readable event log and a task that resolves; a ledger loss drops the Intent and with it the report | the apply cycle that verifies the promotion; every later cycle and the confirmed log (intent ids) | the late report appears in the cycle the model requires, never against a hold (it is not a promotion of that cycle), and no intent id is logged twice |
| `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` | after Forge-faulted apply cycles under lag a dependency written by the prior-merge repair keeps a lifecycle label, a dry run previews T, and the next apply cycle queues it | fair cycle (promotable at both boundaries, no injected failure); a single-Issue read right after a write may return the old value (#902 Round 5) | the label of the dependency after the lagged cycle; the dry-run cycle after two faulted apply cycles; the apply cycle after it | the dependency holds a lifecycle label; T appears in the preview (N = 1) and is queued by the next apply cycle |

### Rows that are not `verified`

- `P1-ESCALATION-NOT-ACTIVE` (unverified): a property of the test's rule choice, not of production code; no fault can be injected
- `P1-EXTERNAL-CHANGE` (out_of_scope): documented non-guarantee (status-labels.md); recovery is Phase 2
- `P2-RECOVERY-STABLE` (unverified): no control: a repair that oscillates after convergence has not been injected
- `P2-CONVERGENCE-UPPER-BOUND` (unverified): no test drives more than one pass; only the expectation (1 cycle) is checked
- `P3A-DOCUMENT-TABLE` (unverified): no control: a document drift has not been injected
- `P3B-LAUNCH-AND-ESCALATION` (unverified): plain assertions without contract ids; no control was injected

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
| `active-fact-guard-bypass` | `_refuse_launch` accepts a different fact while an execution is active | `P3A-LAUNCH-IDENTITY-CONFORMANCE` | deterministic comparison with a different active fact |
| `budget-not-consumed` | `apply_event` keeps the previous retries | `P3B-BUDGET-CONSUMED` | reclaim loop |
| `budget-bound-exceeded` | the reclaim limit is raised above the table | `P3B-RETRY-BOUND` | reclaim loop |
| `persistent-budget-reset` | `restart` also zeroes the persistent counts | `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | ledger-loss scenario |
| `reclaim-record-replaced` | a reclaim reservation, cloud reservation or dirty hold writes a fresh `TaskReclaimRecord` instead of updating the persisted one (the pre-#1279 write) | `P3B-RECLAIM-BUDGET-PRESERVED` | persisted record after the zombie reclaim (budgets and usage-limit run), the cloud reservation and the dirty hold |
| `relaunch-counter-reset` | a normal launch starts `recompute_count` / `forced_serial` from 0 / False (the pre-#1280 launch) | `P3B-RECOMPUTE-BUDGET-PRESERVED` | the active after the normal launch entry (claim reservation, counters saved before the provider, launch record) that deviates again |
| `promotion-suppressed` / `promotion-delayed` | the promotion boundary is skipped always / in the evidence cycle | `P3C-CASE-DELAY` (case table), `P3C-LIVENESS-BOUND`, `P3C-INTERMEDIATE-LIVENESS` | label, `record_completion` (apply), prior merge (apply and dry run) |
| `empty-completion-set` / `throwaway-context` | the #902 Round 4/5 miswiring | `P3C-CASE-DELAY` | case table |
| `prior-merge-repair-exclusion-removed` | `CycleContext.is_prior_merge_repair_attempted` always returns false (the status repair acts on the listing taken before the prior-merge write) | `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` (deterministic scenario), `P3C-LIVENESS-BOUND` (replay of `prior_merge_after_faults`) | the dependency's label after the lagged cycle; the preview of T |
| `report-record-dropped` | the verified promotion is not persisted as an unreported record | `P3C-PROMOTION-REPORTED` (replay of `lagged_chain_reported_later`) | the late report the model requires in the next apply cycle |
| `report-delivered-twice` | the record is neither checked against the confirmed log nor consumed after the append | `P3C-PROMOTION-REPORTED` (replay of `lagged_chain_reported_later`) | an intent id twice in the reported events and the confirmed log |
| `ledger-premise-removed` | the fairness judgement drops the premise that a recompute target has no active ledger entry (#1281 counterexample 2b) | `P3C-LIVENESS-BOUND` | blocked-recompute T whose completed dependency stays in the ledger after a faulted apply cycle (deterministic scenario) |
| `intermediate-ignored` | the intermediate node's assessment is dropped | `P3C-INTERMEDIATE-LIVENESS` | intermediate topology |
| `hold-guard-bypass` (3c) | every `PROMOTION_HOLD_LABELS` is empty | `P3C-SAFETY` | `ci:base-branch-red` before the cycle |
| `reservation-guard-bypass` | the reservation checks of the dependency side, the target side or the intermediate's dependency always pass | `P3C-SAFETY`, `P3C-INTERMEDIATE-SAFETY` (machine, apply cycles), `P3C-DRYRUN-DEPENDENCY-RESERVATION` (deterministic dry run), `P3C-DRYRUN-RESERVATION` (target/intermediate own dry run) | apply cycles in the machine; the dependency side of a dry run in a deterministic scenario; target and intermediate self-reservations in dry run |
| `stale-evidence` | a reopened dependency is still treated as completed | `P3C-SAFETY` | revoked label evidence |

Known gaps, recorded and not weakened: the dependency side of a dry run (fixed in #1267), for T and for an intermediate node, is checked by its own deterministic scenario (`P3C-DRYRUN-DEPENDENCY-RESERVATION`). The previews under T's own reservation and an intermediate node's own reservation are verified by dedicated deterministic scenarios and controls (`P3C-DRYRUN-RESERVATION`, #1283). Two guards (`fresh-guard-bypass` with `status_repair_preserves_protection` alone, and a premature intermediate assessment) are re-validated by a second layer and cannot be bypassed with one fault; the controls inject the whole guard.

<a id="static-coverage"></a>
<!-- static-coverage -->
## Static coverage evidence

Static coverage for `P3A-ROUTE-COVERAGE` is established through a chain of two verifications:

1. **Production call-site scan matches the registry**: Agreement between call sites discovered by production AST scanning and the manually maintained `CALL_SITES` count and set (`tests/test_status_transition_callsites.py::TestRegistryMatchesTheSource::test_every_call_site_is_registered_with_its_call_count`).
2. **Registry matches route table and executed cases**: Agreement between `CALL_SITES` and the `EVENT_BY_SOURCE` route table, and between route conditions and executed cases in `CASES` (`tests/test_status_events.py::TestEventRegistry::test_route_table_covers_every_source_and_executes_every_condition`).

**Premise and observation boundary.** The premise is "syntax scan of production call sites -> `CALL_SITES` counts and set -> `EVENT_BY_SOURCE` routes and executed cases", and the observation boundary is "call site registry and route table".

**AST visitor detection scope and limitations.**
- The AST visitor detects calls where the function name is `transition_status_label` (either as an AST `Name` or as an `Attribute` with that name). Module attribute accesses (e.g. `module.transition_status_label(...)`) match as long as the name matches.
- Aliased calls via renamed imports, indirect calls via variable assignments, and `getattr` or dynamic loading are not tracked. Different functions or methods sharing the same name may result in false positives. This scan makes no claim to be a complete code analysis that resolves symbol bindings.
- The target scope is adapter calls under `orchestune/**/*.py` subject to registration. Direct label manipulations and label-invariant completion paths remain covered by the existing `OUT_OF_SCOPE_PATHS` and invariant-path tables; adding documentation references does not expand the scan scope itself.

<a id="quint-model"></a>
<!-- quint-model -->
## Quint model and production replay

`specs/quint/dependency_liveness.qnt` models the bounded liveness of dependency resolution (#1276). It is **a sampled exploration by the Quint simulator (`quint run`), not a proof**: nothing is claimed about arbitrary graphs, unbounded disturbance sequences or states the sampled sequences did not reach. `quint verify` (Apalache, JVM) is neither required nor used.

**Three layers; none replaces another.**

1. *Model*: the fixed-seed exploration and every saved scenario must keep the model's own invariants (`P3C-QUINT-MODEL`): `safety` (nothing is promoted against an obligation), `liveness` (every fair cycle ends with the promotion, N = 1; apply needs the real label, a dry run this cycle's preview), `eventsAreLabelChanges` (every current event of an apply cycle is a real label change), `labelChangesReported` (a node an apply cycle promoted is reported now or its promotion is outstanding, the converse of the previous one, #1296), `reportsAreEarlierChanges` (a late report concerns an already queued node the model permits and never appears in a dry run) and `dryRunReadOnly`. A late report is not a promotion of its own cycle: `safety` counts the current events only.
2. *Replay*: every transition of the generated and of the saved ITF traces is executed on the production harness (`LivenessWorld`: the real `_prepare_cycle_context` and `execute_pipeline`). After each cycle the production observation (labels, `PromotionEvent` previews) is compared with the model's obligations (a promotion event counts as a promotion even when no label changed; an undisturbed apply cycle's events must be real label changes; a dry run changes no lifecycle label, apart from the fixture's own stale change); before each action the production state is compared with the model's (`P3C-QUINT-OBSERVATION`). Expectations come from the model state only; production is never asked whether a dependency is complete.
3. *Controls*: one model fault each (the fault scenarios run on the correct model and must pass, and on the faulty model must violate exactly the expected invariants at the planned step) and one production fault each (the same trace replayed with the fault must violate the expected contract). A parse, type or runtime error and a timeout are tool errors, never a detection.

**Toolchain.** Node.js (the major version in `package.json` `engines`; the CI uses `actions/setup-node`) is a required dependency of the local CI. `scripts/quint-check.sh` / `scripts/quint-check.ps1` run `npm ci` and verify the pinned `@informalsystems/quint` (exact version in `package.json` and `package-lock.json`); a missing Node.js stops the local CI with exit 2 and the install steps, never a skip. The simulator runs with `--backend=typescript` (it ships inside the pinned package; the default `rust` backend downloads an unpinned binary at first use).

**Exploration and its record.** Every local CI run explores with the same seed and bounds (`EXPLORATION` in `tests/quint_scenarios.py`: seed `0x2f9c`, at most 1500 samples of at most 30 steps, 100 traces written with `--mbt --out-itf --n-traces`) into `.orchestune/tmp/quint-replay-1276-…/` (created by `scripts/create-session-dir.*`, reused in the same run), replays all traces and requires a positive number of traces, transitions, cycles and checked obligations. `exploration-summary.json` in that directory records the tool versions, seed, bounds, duration and counts, and the local CI prints it. A violated invariant is reported with its name, the exact command (seed and backend) and the counterexample ITF trace, which stays in that directory. An empty or truncated output, a broken ITF, an unsupported action or value, or a trace with only an initial state fails the run.

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

**Saved scenarios.** The scripted scenarios of the model (`init_<id>`; one fixed choice per step, so the ITF has the same shape as a random trace) cover final and intermediate nodes, apply and dry run, live and lagged observation, no-op and real changes, dependency-side and target-side holds, failures, restarts, duplicates and stale changes. `tests/fixtures/quint/dependency_liveness_traces.json` stores the ITF with its seed, bounds, Quint version and generation command; a test regenerates each and fails when the model changed without `uv run python -m tests.quint_scenarios regenerate`. `tests/fixtures/quint/dependency_liveness_fault_scenarios.json` stores the model-fault scenarios with the expected invariants and planned step.

**Production counterexamples.** No production defect is pinned at the moment. The counterexample of #1281 (a dependency written by the prior-merge repair loses its last lifecycle label to a same-cycle status repair under lag) was fixed in #1300 and is the ordinary saved scenario `prior_merge_after_faults`. The replay can still pin a future defect: the fixture entry names the Issue, the contract, the step and the node, and only that mismatch is re-labelled, any other mismatch fails (`test_a_known_defect_pin_is_exact`).

| Fault | Model: violated invariant (scenario) | Replay: violated contract (stored scenario) |
|---|---|---|
| `promotion-suppressed` | `liveness` (`final_apply`) | `P3C-LIVENESS-BOUND` (`final_apply`) |
| `promotion-delayed` | `liveness` (`final_apply`) | `P3C-LIVENESS-BOUND` (`final_apply`) |
| `stale-evidence` | `safety` (`stale_revoke`) | `P3C-SAFETY` (`stale_revoke`) |
| `intermediate-ignored` | `liveness` (`chain_apply`) | `P3C-INTERMEDIATE-LIVENESS` (`chain_apply`) |
| `reservation-guard-bypass` | `safety` (`dependency_reservation_apply`) | `P3C-SAFETY` (`dependency_reservation_apply`) |
| `hold-guard-bypass` | `safety` (`base_red_apply`) | `P3C-SAFETY` (`base_red_apply`) |
| `event-only` | `eventsAreLabelChanges`, `liveness` (`final_apply`) | `P3C-LIVENESS-BOUND` (`final_apply`); `P3C-SAFETY` when the event appears against an obligation (`base_red_apply`) |
| `dry-run-writes` | `dryRunReadOnly` (`final_dry`) | `P3C-DRYRUN-READONLY` (`final_dry`) |
| `empty-completion-set` | - | `P3C-LIVENESS-BOUND` (`recompute_release`) |
| `throwaway-context` | - | `P3C-LIVENESS-BOUND` (`lagged_apply`) |
| `report-record-dropped` | - | `P3C-PROMOTION-REPORTED` (`lagged_chain_reported_later`) |
| `report-delivered-twice` | - | `P3C-PROMOTION-REPORTED` (`lagged_chain_reported_later`) |
| `prior-merge-repair-exclusion-removed` | - | `P3C-LIVENESS-BOUND` (`prior_merge_after_faults`) |
| `action-mapping-swapped` | - | `P3C-QUINT-OBSERVATION` (`duplicate_completion`) |
| `action-mapping-dropped` | - | `P3C-SAFETY` (`restart_loses_ledger`) |

## Guarantee and limits

Verified means the tests above fail for the injected faults listed, in the deterministic scenarios described. It does not mean that no other fault exists, that arbitrary graphs or unbounded disturbance sequences are covered, or that the random exploration reaches every state. Production defects found while auditing are split into their own Issues with a counterexample and pinned with a strict xfail; expectations, fairness and bounds are not weakened to make an audit pass.

## Reproduction

```bash
uv run pytest tests/test_verification_contracts.py tests/test_status_machine_stateful.py tests/test_status_reconciliation_stateful.py tests/test_consistency_status_repairs.py tests/test_status_events.py tests/test_status_events_stateful.py tests/test_status_event_retry_resume.py tests/test_dependency_liveness_stateful.py tests/test_status_transition_callsites.py -n0 --hypothesis-profile ci
```

Replay a random failure with `--hypothesis-seed=<seed>`; the control scenarios need no seed.

The Quint model and its replay (Node.js is required; `scripts/quint-check.sh` installs the pinned tools):

```bash
./scripts/quint-check.sh                       # Windows: .\scripts\quint-check.ps1
uv run pytest tests/test_quint_dependency_replay.py -n0
uv run python -m tests.quint_scenarios regenerate   # after changing the model
```

To rerun one scenario by hand, use the command stored beside its trace in `tests/fixtures/quint/dependency_liveness_traces.json` (`node_modules/.bin/quint run specs/quint/dependency_liveness.qnt --backend=typescript --init=init_<id> …`); the exploration command, seed and bounds are in `exploration-summary.json`.
