"""Termination of the budgeted loops on the Event model (#1266, design #1219 §3).

What is checked
---------------

`BudgetTerminationMachine` drives `apply_event` with random sequences of every
(Event, Kind): budget-consuming and free events, re-delivery of the last event,
events of a retired launch (ABA) or of no launch, stops at `Stage.RESERVED` /
`Stage.LABEL_ADDED` and their resume, `restart(ledger_loss)`, time advancing past
backoffs, and external relabelling (Phase 1's epoch approach: after an external
change the reference points of invariants 4 and 5 restart from the new labels).

1. *Loop measure*: a return to queued (or straight back to in-progress) from
   in-progress, done or not-needed - directly, or through a `status:blocked` entered
   from in-progress - either consumes one slot of its budget (a resumed reservation
   consumes none), or is listed in `UNBUDGETED_LOOPS` with a reason.
2. *Bounded within a ledger epoch*: new logical retries per budget never exceed the
   specification table `RETRY_BOUNDS`; `restart(ledger_loss=True)` resets exactly the
   local budgets of `LOCAL_BUDGETS` (documented in `docs/*/setup.md`).
3. *No duplicate active execution*: a launch is never applied while another launch
   is active; the same launch re-delivered is never applied twice.
4. *No relaunch after completion*: a completed task is not launched again unless a
   completion withdrawal (`MERGE_REVERT` / `REVIEW_REJECT`) or an external change
   intervened.
5. *ESCALATION is irreversible*: no automatic event moves an escalated task to an
   ACTIVE label; humans act through external relabelling.

Expected bounds and budget owners come from the #1219 budget table, not from
`plan_retry` / `exceeds_limit` (which `apply_event` itself calls).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    precondition,
    rule,
)

from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import TaskReclaimRecord
from orchestune.ledger.status_events import (
    EVENT_SPECS,
    Applied,
    BudgetLimits,
    Event,
    EventInput,
    ExecutionIdentity,
    Kind,
    Rejected,
    RetryStates,
    Stage,
    TaskModel,
    apply_event,
    restart,
)
from tests.dependency_liveness_test_support import CASE_TABLE, LIVENESS_BOUND
from tests.status_event_test_support import EVENT_BY_SOURCE, production_limits

Q = StatusLabel.QUEUED
B = StatusLabel.BLOCKED
P = StatusLabel.IN_PROGRESS
D = StatusLabel.DONE
N = StatusLabel.NOT_NEEDED
H = StatusLabel.BLOCKED_HUMAN_REVIEW
M = StatusLabel.MANUAL_MERGE_REQUIRED
FS = StatusLabel.FORCE_SERIAL
ACTIVE = frozenset({Q, B, P})
ESCALATION = frozenset({H, M})
#: Labels whose task already ran: a return from them to queued is a loop.
EXECUTED = frozenset({P, D, N})
LIFECYCLE = (Q, B, P, D, N, H, M)
AUXILIARY = (FS, StatusLabel.BLOCKED_RECOMPUTE, StatusLabel.EXTERNAL_LOCK)
LIMITS = production_limits()
_E, _K = Event, Kind

# ---- specification tables ----------------------------------------------------

#: Budgets consumed by a direct requeue, and the `RetryStates` field they live in.
BUDGET_OF: dict[tuple[Event, Kind], str] = {
    (_E.RECLAIM, _K.PLAIN): "reclaim",
    (_E.REQUEUE, _K.EARLY_DEATH): "early_death",
    (_E.REQUEUE, _K.REVIEW_TIMEOUT): "review_timeout",
    (_E.REQUEUE, _K.USAGE_LIMIT): "usage_limit",
}
#: Entries into `status:blocked` from in-progress that consume a persistent budget.
BUDGETED_BLOCKS: dict[tuple[Event, Kind], str] = {
    (_E.BLOCK, _K.BASE_BRANCH_RED): "base_branch_red",
}

#: New logical retries allowed per budget within one ledger epoch, with the default
#: limits: `max_task_reclaims`=3 reclaims requeue, the 4th escalates;
#: `max_early_death_retries`=2 retries; `max_review_timeout_retries`=2 *attempts*,
#: i.e. one requeue; base-branch-red holds on attempts 1-2 and escalates on the 3rd;
#: `max_recompute_retries`=2 recomputations before force-serial.
RETRY_BOUNDS: dict[str, int] = {
    "reclaim": 3,
    "early_death": 2,
    "review_timeout": 1,
    "usage_limit": 2,
    "base_branch_red": 2,
    "recompute": 2,
}

#: Budgets kept only in the local `run_state.json` and lost with it.
LOCAL_BUDGETS = frozenset({"reclaim", "early_death", "review_timeout", "usage_limit"})
#: The `TaskReclaimRecord` fields that hold each local budget.
LOCAL_BUDGET_FIELDS: dict[str, frozenset[str]] = {
    "reclaim": frozenset({"count", "pending"}),
    "early_death": frozenset(
        {"early_death_retry_count", "early_death_retry_at", "early_death_retry_pending"}
    ),
    "review_timeout": frozenset(
        {
            "review_timeout_retry_count",
            "review_timeout_retry_at",
            "review_timeout_retry_pending",
        }
    ),
    "usage_limit": frozenset(
        {
            "usage_limit_retry_count",
            "usage_limit_retry_at",
            "usage_limit_retry_pending",
            "usage_limit_retry_run",
        }
    ),
}


@dataclass(frozen=True)
class UnbudgetedLoop:
    reason: str
    follow_up: str


#: Loops back to execution that no budget bounds, each with the reason it is
#: accepted for now. `BLOCK` entries are the first step of a two-step loop:
#: in-progress -> blocked, then the dependency promotion (or a claim) once every
#: declared dependency is complete (the liveness of #1265).
UNBUDGETED_LOOPS: dict[tuple[Event, Kind], UnbudgetedLoop] = {
    (_E.MERGE_REVERT, _K.PLAIN): UnbudgetedLoop(
        "Integrator rollback after a failed trial-merge CI (done -> queued); "
        "the rollback count is not recorded",
        "budget the rollback (#1219 out-of-scope candidate)",
    ),
    (_E.REQUEUE, _K.RECOVERY): UnbudgetedLoop(
        "requeue of an execution lost by a restart; bounded by the number of "
        "restarts, not by a budget",
        "none",
    ),
    (_E.REVIEW_REJECT, _K.PLAIN): UnbudgetedLoop(
        "an independent review rejected a not-needed completion (not-needed -> "
        "queued, added by #1264); every round needs one more independent review",
        "budget the rejection",
    ),
    (_E.BLOCK, _K.PLAIN): UnbudgetedLoop(
        "launch failure or a blocked outcome hold (in-progress -> blocked); with "
        "every dependency complete the next cycle promotes it again",
        "budget repeated launch failures / blocked outcomes",
    ),
    (_E.BLOCK, _K.COMPLETION): UnbudgetedLoop(
        "`orchestune complete --result blocked` (in-progress -> blocked); "
        "promoted again like any other blocked task",
        "budget repeated launch failures / blocked outcomes",
    ),
    (_E.BLOCK, _K.RECOMPUTE): UnbudgetedLoop(
        "blocked by another task's footprint deviation; bounded by that task's "
        "recompute budget, which the single-task model cannot see (and which "
        "restarts on relaunch, #1280)",
        "#1280",
    ),
}


def returns_to_execution(event: Event, kind: Kind) -> bool:
    """Whether the spec lets (event, kind) go from an executed label to Q or P."""
    spec = EVENT_SPECS[(event, kind)]
    return spec.target in (Q, P) and bool(spec.sources & EXECUTED)


def leaves_execution_to_blocked(event: Event, kind: Kind) -> bool:
    spec = EVENT_SPECS[(event, kind)]
    return spec.target is B and P in spec.sources


# ---- static checks over the production routes ---------------------------------


def _production_routes() -> set[tuple[Event, Kind]]:
    return {(r.event, r.kind) for routes in EVENT_BY_SOURCE.values() for r in routes}


class TestLoopRegistry:
    def test_every_production_loop_is_budgeted_or_listed(self) -> None:
        loops = {
            key
            for key in _production_routes()
            if returns_to_execution(*key) or leaves_execution_to_blocked(*key)
        }
        unbounded = loops - BUDGET_OF.keys() - BUDGETED_BLOCKS.keys()
        assert unbounded == UNBUDGETED_LOOPS.keys(), sorted(unbounded)

    def test_listed_loops_exist_and_carry_reasons(self) -> None:
        for key, loop in UNBUDGETED_LOOPS.items():
            assert key in _production_routes(), key
            assert loop.reason and loop.follow_up

    def test_the_two_design_loops_are_listed(self) -> None:
        # #1219 §3 fixed these two initially; the others were found by #1266.
        assert {(_E.MERGE_REVERT, _K.PLAIN), (_E.REQUEUE, _K.RECOVERY)} <= set(
            UNBUDGETED_LOOPS
        )

    def test_ledger_loss_resets_exactly_the_local_budgets(self) -> None:
        state = TaskModel.from_labels((P,))
        spent = replace(
            state,
            retries=RetryStates(
                reclaim=replace(state.retries.reclaim, count=2, pending=True),
                early_death=replace(state.retries.early_death, count=1, retry_at=9.0),
                review_timeout=replace(state.retries.review_timeout, count=1),
                usage_limit=replace(state.retries.usage_limit, count=1),
            ),
            counts=replace(state.counts, recompute=2, base_branch_red=1),
        )
        lost = restart(spent, ledger_loss=True)
        reset = {
            f.name
            for f in fields(RetryStates)
            if getattr(lost.retries, f.name) != getattr(spent.retries, f.name)
        }
        assert reset == LOCAL_BUDGETS
        assert lost.counts == spent.counts

    def test_local_budget_fields_cover_the_ledger_record(self) -> None:
        diagnostic = {"last_reclaimed_at"}
        record_fields = {f.name for f in fields(TaskReclaimRecord)} - diagnostic
        assert set().union(*LOCAL_BUDGET_FIELDS.values()) == record_fields


# ---- the stateful machine ------------------------------------------------------


def _budget(retries: RetryStates, name: str) -> Any:
    return getattr(retries, name)


def _new_returns(before: TaskModel, after: TaskModel) -> frozenset[StatusLabel]:
    return (after.lifecycle & {Q, P}) - before.lifecycle


#: Most deliveries run to the end; the rest stop at a reservation or after the add.
_STOPS = st.sampled_from([None, None, None, None, Stage.RESERVED, Stage.LABEL_ADDED])
#: Events that consume a budget.
_BUDGETED_KEYS = sorted({*BUDGET_OF, *BUDGETED_BLOCKS, (_E.RECOMPUTE, _K.PLAIN)})
#: Events that complete, withdraw or loop back, so that loops are reachable.
_LOOP_KEYS = sorted(
    {
        *UNBUDGETED_LOOPS,
        (_E.QUEUE, _K.PLAIN),
        (_E.QUEUE, _K.RECOMPUTE),
        (_E.QUEUE, _K.BASE_BRANCH_RED),
        (_E.COMPLETE, _K.PLAIN),
        (_E.NOT_NEEDED, _K.PLAIN),
        (_E.ESCALATE, _K.PLAIN),
    }
)


class BudgetTerminationMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.limits: BudgetLimits = LIMITS
        self.state = TaskModel.from_labels((Q,))
        self.now = 1_000.0
        self.launches = 0
        self.operations = 0
        self.last: EventInput | None = None
        #: (event, kind) that moved the task from in-progress to blocked.
        self.blocked_by: tuple[Event, Kind] | None = None
        #: Completed since the last withdrawal / external change (invariant 4).
        self.completed = False
        #: New logical retries per budget in the current ledger epoch (invariant 2).
        self.retries_in_epoch: dict[str, int] = dict.fromkeys(RETRY_BOUNDS, 0)

    # -- helpers ---------------------------------------------------------------
    def _identity(self, data: st.DataObject, launch: bool) -> Any:
        """The launch an event claims; the current one is the most likely."""
        current = self.state.execution_identity
        retired = sorted(
            self.state.retired_execution_identities,
            key=lambda e: getattr(e, "launch_id", ""),
        )
        if launch:
            self.launches += 1
            fresh = ExecutionIdentity(f"launch-{self.launches}")
            choices: list[Any] = [fresh, fresh, fresh, current, *retired[-1:]]
        else:
            choices = [current, current, current, None, *retired[-1:]]
        return data.draw(st.sampled_from(choices), label="execution")

    def _operation(self, data: st.DataObject) -> str:
        pending = self.state.pending_operation
        if pending is not None and data.draw(st.booleans(), label="resume"):
            return pending.operation
        return self._new_operation()

    def launch_for_test(self) -> None:
        self.launches += 1
        identity = ExecutionIdentity(f"launch-{self.launches}")
        event = EventInput(_E.LAUNCH, _K.PLAIN, identity, self._new_operation())
        self._deliver(event)

    def _new_operation(self) -> str:
        self.operations += 1
        return f"op-{self.operations}"

    def _deliver(self, event: EventInput) -> None:
        before = self.state
        result = apply_event(before, event, self.limits)
        self.last = event
        self._check_delivery(before, event, result)
        if isinstance(result, Applied):
            self._account(before, event, result)
            self.state = result.state

    # -- invariants checked on every delivery ------------------------------------
    def _check_delivery(
        self, before: TaskModel, event: EventInput, result: Any
    ) -> None:
        key = (event.event, event.kind)
        applied = isinstance(result, Applied)
        if (
            event.operation is not None
            and event.operation in before.confirmed_operations
        ):
            assert not applied, "a confirmed operation was re-applied"
        if event.event is Event.LAUNCH:
            # Invariant 3: one active execution; a re-delivered launch is not applied.
            if before.execution_identity is not None:
                assert not applied, "launch over an active execution"
            # Invariant 4: completed tasks are not launched again.
            if before.completion_done or self.completed:
                assert not applied, "launch after completion"
        elif (
            event.execution is not None and event.execution != before.execution_identity
        ):
            assert isinstance(result, Rejected), "event of a stale execution applied"
        if applied and before.lifecycle & ESCALATION:
            # Invariant 5: no automatic event leaves ESCALATION for ACTIVE.
            assert not result.state.lifecycle & ACTIVE - before.lifecycle, key
        if applied and key in BUDGET_OF:
            self._check_budget_step(before, event, result)

    def _check_budget_step(
        self, before: TaskModel, event: EventInput, result: Applied
    ) -> None:
        """Invariant 1: a new delivery consumes one slot, a resume consumes none."""
        name = BUDGET_OF[(event.event, event.kind)]
        old, new = _budget(before.retries, name), _budget(result.state.retries, name)
        pending = before.pending_operation
        resumed = old.pending or (
            pending is not None and pending.operation == event.operation
        )
        if resumed:
            assert new.count == old.count, (name, old, new)
            assert getattr(new, "retry_at", None) == getattr(old, "retry_at", None)
        elif result.escalated and name != "reclaim":
            assert new.count == old.count, (name, old, new)  # exhausted: no slot
        else:
            assert new.count == old.count + 1, (name, old, new)

    def _account(self, before: TaskModel, event: EventInput, result: Applied) -> None:
        key, after = (event.event, event.kind), result.state
        returned = _new_returns(before, after)
        if returned and before.lifecycle & EXECUTED:
            assert key in BUDGET_OF or key in UNBUDGETED_LOOPS, key
        elif returned and B in before.lifecycle and self.blocked_by is not None:
            # Second step of in-progress -> blocked -> queued / in-progress.
            entry = self.blocked_by
            assert entry in BUDGETED_BLOCKS or entry in UNBUDGETED_LOOPS, entry
        if key in BUDGET_OF and Q in returned:
            self._count(BUDGET_OF[key])
        pending = before.pending_operation
        resumed = pending is not None and pending.operation == event.operation
        if not resumed and not result.escalated:
            if key in BUDGETED_BLOCKS:
                self._count(BUDGETED_BLOCKS[key])
            if key == (_E.RECOMPUTE, _K.PLAIN):
                self._count("recompute")
        if B not in after.lifecycle:
            self.blocked_by = None
        elif B not in before.lifecycle:
            self.blocked_by = key if P in before.lifecycle else None
        if after.completion_done and not before.completion_done:
            self.completed = True
        if event.event in (_E.MERGE_REVERT, _E.REVIEW_REJECT):
            self.completed = False

    def _count(self, budget: str) -> None:
        self.retries_in_epoch[budget] += 1
        # Invariant 2: bounded within the ledger epoch.
        assert self.retries_in_epoch[budget] <= RETRY_BOUNDS[budget], (
            budget,
            self.retries_in_epoch,
        )

    # -- setup -------------------------------------------------------------------
    @initialize(
        lifecycle=st.sampled_from([(Q,), (B,), (P,)]),
        auxiliary=st.sets(st.sampled_from(AUXILIARY), max_size=1),
    )
    def start(self, lifecycle: tuple[str, ...], auxiliary: set[str]) -> None:
        self.state = TaskModel.from_labels((*lifecycle, *auxiliary))

    # -- rules -------------------------------------------------------------------
    @rule(data=st.data(), kind=st.sampled_from([_K.PLAIN, _K.CLAIM, _K.RECOVERY]))
    def launch(self, data: st.DataObject, kind: Kind) -> None:
        execution = self._identity(data, launch=True)
        self._deliver(
            EventInput(Event.LAUNCH, kind, execution, self._operation(data), self.now)
        )

    @rule(
        data=st.data(),
        key=st.sampled_from(_BUDGETED_KEYS),
        stop=_STOPS,
    )
    def budgeted(
        self, data: st.DataObject, key: tuple[Event, Kind], stop: Stage | None
    ) -> None:
        execution = self._identity(data, launch=False)
        event = EventInput(*key, execution, self._operation(data), self.now, stop)
        self._deliver(event)

    @rule(key=st.sampled_from(_BUDGETED_KEYS + _LOOP_KEYS), times=st.integers(1, 4))
    def retry_round(self, key: tuple[Event, Kind], times: int) -> None:
        """`times` loop iterations: launch when idle, then `key` for that launch."""
        for _ in range(times):
            if self.state.pending_operation is None and self.state.lifecycle <= {Q, B}:
                self.launches += 1
                self._deliver(
                    EventInput(
                        _E.LAUNCH,
                        _K.PLAIN,
                        ExecutionIdentity(f"launch-{self.launches}"),
                        self._new_operation(),
                        self.now,
                    )
                )
            execution = self.state.execution_identity
            self._deliver(EventInput(*key, execution, self._new_operation(), self.now))

    @rule(data=st.data(), key=st.sampled_from(_LOOP_KEYS))
    def loop_step(self, data: st.DataObject, key: tuple[Event, Kind]) -> None:
        execution = self._identity(data, launch=False)
        self._deliver(EventInput(*key, execution, self._operation(data), self.now))

    @rule(
        data=st.data(),
        key=st.sampled_from(sorted(EVENT_SPECS)),
        stop=_STOPS,
    )
    def any_event(
        self, data: st.DataObject, key: tuple[Event, Kind], stop: Stage | None
    ) -> None:
        execution = self._identity(data, launch=key[0] is Event.LAUNCH)
        self._deliver(
            EventInput(*key, execution, self._operation(data), self.now, stop)
        )

    @precondition(lambda self: self.state.pending_operation is not None)
    @rule(stop=st.sampled_from([None, None, Stage.LABEL_ADDED]))
    def resume(self, stop: Stage | None) -> None:
        pending = self.state.pending_operation
        assert pending is not None
        execution = self.state.execution_identity
        self._deliver(
            EventInput(
                pending.event,
                pending.kind,
                execution,
                pending.operation,
                self.now,
                stop,
            )
        )

    @precondition(lambda self: self.last is not None)
    @rule()
    def resend_last(self) -> None:
        assert self.last is not None
        self._deliver(self.last)

    @rule(seconds=st.sampled_from([1.0, 59.0, 61.0, 240.0]))
    def advance_time(self, seconds: float) -> None:
        self.now += seconds

    @rule(ledger_loss=st.booleans())
    def restart(self, ledger_loss: bool) -> None:
        self.state = restart(self.state, ledger_loss=ledger_loss)
        if not self.state.completion_done:
            self.completed = False
        if ledger_loss:
            for budget in LOCAL_BUDGETS:
                self.retries_in_epoch[budget] = 0

    @rule(
        lifecycle=st.sets(st.sampled_from(LIFECYCLE), min_size=1, max_size=2),
        auxiliary=st.sets(st.sampled_from(AUXILIARY), max_size=1),
    )
    def external_relabel(self, lifecycle: set[str], auxiliary: set[str]) -> None:
        relabelled = TaskModel.from_labels((*lifecycle, *auxiliary))
        self.state = replace(
            self.state,
            lifecycle=relabelled.lifecycle,
            auxiliary=relabelled.auxiliary,
            pending_operation=None,
        )
        self.completed = self.state.completion_done
        self.blocked_by = None


TestBudgetTerminationMachine = BudgetTerminationMachine.TestCase


#: Each budgeted loop driven past its bound, and the terminal state it ends in.
_BOUNDED_LOOPS = [
    ((_E.RECLAIM, _K.PLAIN), "reclaim", {H}),
    ((_E.REQUEUE, _K.EARLY_DEATH), "early_death", {H}),
    ((_E.REQUEUE, _K.REVIEW_TIMEOUT), "review_timeout", {H}),
    ((_E.REQUEUE, _K.USAGE_LIMIT), "usage_limit", {H}),
    ((_E.BLOCK, _K.BASE_BRANCH_RED), "base_branch_red", {H}),
    ((_E.RECOMPUTE, _K.PLAIN), "recompute", {P}),
]


class TestBudgetBounds:
    """Deterministic counterparts of invariants 1 and 2 for every budget."""

    @pytest.mark.parametrize(("key", "budget", "terminal"), _BOUNDED_LOOPS)
    def test_loop_stops_at_the_table_bound(
        self, key: tuple[Event, Kind], budget: str, terminal: set[StatusLabel]
    ) -> None:
        machine = BudgetTerminationMachine()
        machine.retry_round(key, RETRY_BOUNDS[budget] + 2)
        assert machine.retries_in_epoch[budget] == RETRY_BOUNDS[budget]
        assert machine.state.lifecycle == terminal
        if budget == "recompute":
            assert FS in machine.state.auxiliary

    def test_ledger_loss_starts_a_new_epoch_for_local_budgets_only(self) -> None:
        machine = BudgetTerminationMachine()
        machine.retry_round((_E.RECLAIM, _K.PLAIN), 3)
        machine.retry_round((_E.BLOCK, _K.BASE_BRANCH_RED), 1)
        machine.restart(ledger_loss=True)
        assert machine.retries_in_epoch["reclaim"] == 0
        assert machine.retries_in_epoch["base_branch_red"] == 1
        machine.retry_round((_E.RECLAIM, _K.PLAIN), 3)
        assert machine.retries_in_epoch["reclaim"] == 3
        assert machine.state.retries.reclaim.count == 3

    def test_a_resumed_reservation_consumes_no_slot(self) -> None:
        machine = BudgetTerminationMachine()
        machine.retry_round((_E.RECLAIM, _K.PLAIN), 1)
        machine.launch_for_test()
        execution = machine.state.execution_identity
        stopped = EventInput(
            _E.RECLAIM, _K.PLAIN, execution, "op-r", 0.0, Stage.RESERVED
        )
        machine._deliver(stopped)
        machine.restart(ledger_loss=False)
        machine._deliver(EventInput(_E.RECLAIM, _K.PLAIN, None, "op-r2", 0.0))
        assert machine.state.retries.reclaim.count == 2
        assert machine.retries_in_epoch["reclaim"] == 2


def test_configured_profile_applies_to_budget_machine() -> None:
    applied: Any = TestBudgetTerminationMachine.settings
    profile = os.environ.get("HYPOTHESIS_PROFILE", "ci")
    assert settings.get_current_profile_name() == profile
    if profile == "ci":
        assert applied.deadline is None and applied.print_blob


# ---- documentation -------------------------------------------------------------

_DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"
_LEDGER_ROW = re.compile(r"^\|[^|\n]*`task_reclaim_counts`[^|\n]*\|([^\n]*)\|$", re.M)
_LOOP_ROW = re.compile(r"^\|\s*`([A-Z_]+)`\s*/\s*`([A-Z_]+)`\s*\|", re.M)
_LIVENESS_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|([^|\n]*)\|([^|\n]*)\|", re.M)


def _doc(lang: str, name: str) -> str:
    return (_DOCS_ROOT / lang / name).read_text(encoding="utf-8")


def _section(doc: str, marker: str) -> str:
    start = doc.index(marker)
    end = doc.find("\n## ", start + 1)
    return doc[start : None if end < 0 else end]


@pytest.mark.parametrize("lang", ["ja", "en"])
class TestDocuments:
    def test_setup_local_state_row_lists_the_ledger_loss_resets(
        self, lang: str
    ) -> None:
        rows = _LEDGER_ROW.findall(_doc(lang, "setup.md"))
        assert len(rows) == 1, rows
        documented = set(re.findall(r"`([a-z_]+(?:_\*)?)`", rows[0]))
        fields_named = {name.removesuffix("_*") for name in documented}
        prefixes = {
            "reclaim": "count",
            "early_death": "early_death_retry",
            "review_timeout": "review_timeout_retry",
            "usage_limit": "usage_limit_retry",
        }
        assert {b for b, p in prefixes.items() if p in fields_named} == LOCAL_BUDGETS

    def test_status_labels_lists_the_unbudgeted_loops(self, lang: str) -> None:
        section = _section(
            _doc(lang, "status-labels.md"), "<!-- budget-termination -->"
        )
        rows = {(Event[e], Kind[k]) for e, k in _LOOP_ROW.findall(section)}
        assert rows == UNBUDGETED_LOOPS.keys()

    def test_status_labels_liveness_table_matches_the_case_table(
        self, lang: str
    ) -> None:
        section = _section(
            _doc(lang, "status-labels.md"), "<!-- dependency-liveness -->"
        )
        rows = {
            name: delay.strip() for name, _, delay in _LIVENESS_ROW.findall(section)
        }
        assert rows.keys() == {case.name for case in CASE_TABLE}
        for case in CASE_TABLE:
            delay = rows[case.name]
            if case.expected_delay is None:
                assert delay.startswith("-") or "なし" in delay or "none" in delay
            else:
                assert delay.startswith(str(case.expected_delay)), (case.name, delay)
            if case.known_bug:
                assert case.known_bug in delay, case.name
        assert f"N={LIVENESS_BOUND}" in section
