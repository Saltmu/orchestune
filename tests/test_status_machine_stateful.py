"""Stateful safety tests for `transition_status_label` (#1217).

What is guaranteed, and under which assumptions
-----------------------------------------------

A. *No external change* (a single, unrecovered operation at a time):

* the Issue keeps at least one lifecycle label before and after every operation
  of this system, including after a stop caused by an injected failure;
* an operation with a complete removal list that finishes normally leaves exactly
  the target lifecycle label;
* after a partial failure, **one** complete, successful retry (add, callback and
  every remove succeed) converges to the target alone, and replaying the same
  operation afterwards leaves the label set unchanged (the callback may run again:
  label idempotency is not exactly-once callbacks);
* normal rules never choose an ESCALATION -> ACTIVE transition. The adapter does
  not refuse it itself.

B. *Arbitrary external change* (`external_relabel` may delete every lifecycle
label, add several, or add an ESCALATION label): no unconditional "at least one" or
"exactly one" is required right after it. The model bumps an epoch, drops the old
pending retry from the automatic rules, and resumes the checks from a new snapshot
that satisfies A's preconditions. This is a boundary of the test, not a production
generation counter.

Not covered: infinitely repeating failures, the global liveness of the scheduler,
other adapters and direct Forge paths (see `test_status_transition_callsites.py`),
and recovery after external changes (#1218).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from orchestune.forge import Forge
from orchestune.labels import StatusLabel
from orchestune.ledger import status_labels
from orchestune.ledger.status_machine import (
    LABEL_ROLES,
    LIFECYCLE_ROLES,
    LabelRole,
    TransitionPlan,
    is_allowed,
    lifecycle_labels,
    plan_transition,
)
from tests.verification_contract_test_support import expect_violation, require

ISSUE = 1
LIFECYCLE = tuple(
    label for label in StatusLabel if LABEL_ROLES[label] in LIFECYCLE_ROLES
)
AUXILIARY = tuple(
    label for label in StatusLabel if LABEL_ROLES[label] is LabelRole.AUXILIARY
)
ACTIVE = tuple(label for label in StatusLabel if LABEL_ROLES[label] is LabelRole.ACTIVE)

#: Start states weighted towards ones that admit a normal transition: one or two
#: ACTIVE labels (the second is a stale leftover to repair), sometimes any pair.
_ACTIVE_STARTS = st.sets(st.sampled_from(ACTIVE), min_size=1, max_size=2)
_ANY_STARTS = st.sets(st.sampled_from(LIFECYCLE), min_size=1, max_size=2)
_LIFECYCLE_STARTS = st.one_of(_ACTIVE_STARTS, _ACTIVE_STARTS, _ANY_STARTS)
#: One-shot failure plans: (operation, before/after the change took effect, nth call).
_FAILURE_PLANS = st.none() | st.tuples(
    st.sampled_from(("add", "remove")),
    st.sampled_from(("before", "after")),
    st.integers(0, 3),
)


class Injected(Exception):
    """The failure the fake Forge raises on purpose."""


class FaultyForge:
    """A label store whose next `add`/`remove` call number `nth` can fail once.

    `before` fails without effect; `after` applies the change and then raises, like
    a response lost after the server accepted the request.
    """

    def __init__(self, labels: set[str] | None = None) -> None:
        self.labels: set[str] = set(labels or ())
        self.armed: tuple[str, str, int] | None = None
        self.calls = {"add": 0, "remove": 0}

    def arm(self, op: str, mode: str, nth: int) -> None:
        self.armed = (op, mode, nth)

    def disarm(self) -> None:
        self.armed = None

    def begin_application(self) -> None:
        self.calls = {"add": 0, "remove": 0}

    def _failure(self, op: str) -> str | None:
        index = self.calls[op]
        self.calls[op] += 1
        if self.armed is not None and self.armed[0] == op and self.armed[2] == index:
            mode = self.armed[1]
            self.armed = None
            return mode
        return None

    def add_label(self, issue_number: int | str, label: str) -> None:
        mode = self._failure("add")
        if mode == "before":
            raise Injected("add")
        self.labels.add(label)
        if mode == "after":
            raise Injected("add")

    def remove_label(self, issue_number: int | str, label: str) -> None:
        mode = self._failure("remove")
        if mode == "before":
            raise Injected("remove")
        self.labels.discard(label)
        if mode == "after":
            raise Injected("remove")


class ObservingForge(FaultyForge):
    """Checks the lifecycle right after every add/remove that took effect (#1275).

    The check runs at each Forge operation boundary, not only when the adapter
    returns, so an operation order that empties the lifecycle in between is
    caught even when the final labels are correct.  It lives in the test Forge:
    the production adapter is never bypassed.
    """

    def __init__(self, labels: set[str] | None = None) -> None:
        super().__init__(labels)
        self.enforce = True

    def _observe(self) -> None:
        if self.enforce:
            require(
                "P1-LIFECYCLE-NONEMPTY",
                lifecycle_labels(self.labels),
                f"no lifecycle label after an operation: {sorted(self.labels)}",
            )

    def add_label(self, issue_number: int | str, label: str) -> None:
        try:
            super().add_label(issue_number, label)
        finally:
            self._observe()

    def remove_label(self, issue_number: int | str, label: str) -> None:
        try:
            super().remove_label(issue_number, label)
        finally:
            self._observe()


def _forge(fake: FaultyForge) -> Forge:
    return cast("Forge", fake)


def transition_status_label(*args: Any, **kwargs: Any) -> None:
    """The production adapter, resolved per call so a control can swap it."""
    status_labels.transition_status_label(*args, **kwargs)


@dataclass
class Pending:
    """The one operation a retry may replay: a fixed target and removal snapshot."""

    target: StatusLabel
    old: tuple[str, ...]
    epoch: int
    done: bool = False


class StatusLabelMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.forge = ObservingForge()
        self.epoch = 0
        self.pending: Pending | None = None
        self.constrained = True
        self.auxiliary: frozenset[str] = frozenset()
        self.callback_armed = False
        self.callback_calls = 0

    # -- helpers ---------------------------------------------------------
    def _lifecycle(self) -> frozenset[StatusLabel]:
        return lifecycle_labels(self.forge.labels)

    def _targets(self) -> list[StatusLabel]:
        held = self._lifecycle()
        return [t for t in LIFECYCLE if held and all(is_allowed(s, t) for s in held)]

    def _callback(self) -> None:
        self.callback_calls += 1
        if self.callback_armed:
            raise Injected("callback")

    def _apply(self, pending: Pending) -> bool:
        """Run the adapter once; True when it returned without raising."""
        self.forge.begin_application()
        self.forge.enforce = self.constrained
        try:
            transition_status_label(
                _forge(self.forge), ISSUE, pending.target, pending.old, self._callback
            )
        except Injected:
            return False
        finally:
            self.forge.disarm()
            self.callback_armed = False
        return True

    def _replayable(self) -> bool:
        return self.pending is not None and self.pending.epoch == self.epoch

    def _may_start(self) -> bool:
        blocked = self._replayable() and not (self.pending and self.pending.done)
        return not blocked and bool(self._targets())

    # -- setup -----------------------------------------------------------
    @initialize(
        lifecycle=_LIFECYCLE_STARTS,
        auxiliary=st.sets(st.sampled_from(AUXILIARY)),
    )
    def start(self, lifecycle: set[StatusLabel], auxiliary: set[StatusLabel]) -> None:
        self.forge.labels = {*lifecycle, *auxiliary}
        self.auxiliary = frozenset(auxiliary)

    # -- rules (A) -------------------------------------------------------
    @precondition(lambda self: self._may_start())
    @rule(data=st.data(), failure=_FAILURE_PLANS)
    def transition(
        self, data: st.DataObject, failure: tuple[str, str, int] | None
    ) -> None:
        held = self._lifecycle()
        target = data.draw(st.sampled_from(self._targets()), label="target")
        assert not any(
            LABEL_ROLES[s] is LabelRole.ESCALATION
            and LABEL_ROLES[target] is LabelRole.ACTIVE
            for s in held
        ), "normal rules must not pick ESCALATION -> ACTIVE"
        extra = data.draw(st.lists(st.sampled_from(LIFECYCLE), max_size=3))
        candidates = [*(label for label in held if label != target), *extra]
        old = tuple(data.draw(st.permutations(candidates), label="old_labels"))
        pending = Pending(target, old, self.epoch)
        self.pending = pending
        self.constrained = True
        if failure is not None and self.forge.armed is None:
            # Arming inline makes "a failure at this position of this operation"
            # likely within the CI example budget; `fail_next` and
            # `crash_after_add` still arm separately before any later operation.
            self.forge.arm(*failure)
        if self._apply(pending):
            pending.done = True
            require(
                "P1-SUCCESS-TARGET-ONLY",
                self._lifecycle() == {target},
                self.forge.labels,
            )

    @precondition(lambda self: self.forge.armed is None and not self.callback_armed)
    @rule(
        op=st.sampled_from(("add", "remove")),
        mode=st.sampled_from(("before", "after")),
        nth=st.integers(0, 3),
    )
    def fail_next(self, op: str, mode: str, nth: int) -> None:
        self.forge.arm(op, mode, nth)

    @precondition(lambda self: self.forge.armed is None and not self.callback_armed)
    @rule()
    def crash_after_add(self) -> None:
        self.callback_armed = True

    @precondition(lambda self: self._replayable())
    @rule()
    def retry_last(self) -> None:
        pending = self.pending
        assert pending is not None
        if self._apply(pending):
            pending.done = True
            require(
                "P1-RETRY-CONVERGES",
                self._lifecycle() == {pending.target},
                self.forge.labels,
            )

    @precondition(lambda self: self._replayable())
    @rule()
    def recover_last(self) -> None:
        pending = self.pending
        assert pending is not None
        self.forge.disarm()
        self.callback_armed = False
        assert self._apply(pending)
        pending.done = True
        require(
            "P1-RETRY-CONVERGES",
            self._lifecycle() == {pending.target},
            self.forge.labels,
        )
        converged = frozenset(self.forge.labels)
        assert self._apply(pending)
        require(
            "P1-RETRY-CONVERGES",
            frozenset(self.forge.labels) == converged,
            "replay changed the labels",
        )

    # -- rule (B) --------------------------------------------------------
    @rule(
        lifecycle=st.one_of(
            _LIFECYCLE_STARTS,
            st.sets(st.sampled_from(LIFECYCLE), max_size=4),
        ),
        auxiliary=st.sets(st.sampled_from(AUXILIARY)),
    )
    def external_relabel(
        self, lifecycle: set[StatusLabel], auxiliary: set[StatusLabel]
    ) -> None:
        labels = {*lifecycle, *auxiliary}
        self.forge.labels = set(labels)
        self.forge.disarm()
        self.callback_armed = False
        self.epoch += 1
        self.pending = None
        self.constrained = False
        self.auxiliary = frozenset(label for label in labels if label in AUXILIARY)

    # -- invariants ------------------------------------------------------
    @invariant()
    def lifecycle_is_never_empty_without_external_change(self) -> None:
        if self.constrained:
            require("P1-LIFECYCLE-NONEMPTY", self._lifecycle(), self.forge.labels)

    @invariant()
    def auxiliary_labels_are_never_touched_by_the_adapter(self) -> None:
        assert {label for label in self.forge.labels if label in AUXILIARY} == set(
            self.auxiliary
        )


TestStatusLabelMachine = StatusLabelMachine.TestCase


# -- deterministic recovery, one case per failure position --------------------

_Q, _B, _P = StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS
_FS = StatusLabel.FORCE_SERIAL
_START = {_Q, _B, _FS}
_OLD = (_Q, _B)


@dataclass(frozen=True)
class _Position:
    name: str
    op: str | None  # "add" | "remove" | None (= the callback)
    mode: str
    nth: int
    expected: frozenset[str]  # labels left right after the interrupted attempt


def _both(*labels: StatusLabel) -> frozenset[str]:
    return frozenset(labels)


_POSITIONS = (
    _Position("add-fails-before-effect", "add", "before", 0, _both(_Q, _B, _FS)),
    _Position("add-response-lost", "add", "after", 0, _both(_Q, _B, _FS, _P)),
    _Position("callback-fails-after-add", None, "", 0, _both(_Q, _B, _FS, _P)),
    _Position("first-remove-fails", "remove", "before", 0, _both(_Q, _B, _FS, _P)),
    _Position("first-remove-response-lost", "remove", "after", 0, _both(_B, _FS, _P)),
    _Position("second-remove-fails", "remove", "before", 1, _both(_B, _FS, _P)),
    _Position("second-remove-response-lost", "remove", "after", 1, _both(_FS, _P)),
)


def _interrupted_forge(position: _Position) -> tuple[ObservingForge, list[str], Any]:
    fake = ObservingForge({*_START})
    calls: list[str] = []

    def callback() -> None:
        calls.append("callback")
        if position.op is None and len(calls) == 1:
            raise Injected("callback")

    if position.op is not None:
        fake.arm(position.op, position.mode, position.nth)
    return fake, calls, callback


def scenario_success_leaves_only_the_target() -> None:
    """A complete removal list leaves the target alone (P1-SUCCESS-TARGET-ONLY)."""
    fake = ObservingForge({_Q, _B, _FS})
    transition_status_label(_forge(fake), ISSUE, _P, _OLD)
    require(
        "P1-SUCCESS-TARGET-ONLY",
        lifecycle_labels(fake.labels) == {_P} and _FS in fake.labels,
        sorted(fake.labels),
    )


def scenario_single_lifecycle_label_is_never_dropped() -> None:
    """One lifecycle label held: it is never the only one removed (P1-LIFECYCLE-NONEMPTY)."""
    fake = ObservingForge({_Q, _FS})
    transition_status_label(_forge(fake), ISSUE, _P, (_Q,))
    require(
        "P1-SUCCESS-TARGET-ONLY", lifecycle_labels(fake.labels) == {_P}, fake.labels
    )


def scenario_one_retry_converges(position: _Position) -> None:
    """Interrupt at ``position``, retry once completely (P1-RETRY-CONVERGES)."""
    fake, calls, callback = _interrupted_forge(position)

    with pytest.raises(Injected):
        transition_status_label(_forge(fake), ISSUE, _P, _OLD, callback)

    # The interrupted attempt always leaves a discoverable lifecycle label.
    assert set(fake.labels) == position.expected
    require("P1-LIFECYCLE-NONEMPTY", lifecycle_labels(fake.labels), fake.labels)

    fake.disarm()
    transition_status_label(_forge(fake), ISSUE, _P, _OLD, callback)

    require(
        "P1-RETRY-CONVERGES",
        fake.labels == {_P, _FS} and lifecycle_labels(fake.labels) == {_P},
        sorted(fake.labels),
    )

    # Replaying the same operation never changes the label set again; the
    # callback may run again (label idempotency is not exactly-once callbacks).
    transition_status_label(_forge(fake), ISSUE, _P, _OLD, callback)
    require("P1-RETRY-CONVERGES", fake.labels == {_P, _FS}, sorted(fake.labels))
    assert len(calls) >= 2


@pytest.mark.parametrize("position", _POSITIONS, ids=lambda p: p.name)
def test_one_complete_retry_converges_after_each_failure_position(
    position: _Position,
) -> None:
    scenario_one_retry_converges(position)


def test_complete_removal_list_leaves_only_the_target() -> None:
    scenario_success_leaves_only_the_target()
    scenario_single_lifecycle_label_is_never_dropped()


# -- controls: one injected adapter fault each (#1275) -------------------------


def _drop_first_remove(new_label: str, old_labels: tuple[str, ...]) -> TransitionPlan:
    plan = plan_transition(new_label, old_labels)
    return TransitionPlan(add=plan.add, remove=plan.remove[1:])


def _remove_before_add(
    forge: Forge,
    issue_number: int | str,
    new_label: str,
    old_labels: Any,
    on_label_added: Any = None,
) -> None:
    for old_label in old_labels:
        for label in plan_transition(new_label, (old_label,)).remove:
            forge.remove_label(issue_number, label)
    forge.add_label(issue_number, plan_transition(new_label, ()).add)
    if on_label_added is not None:
        on_label_added()


def test_control_remove_missing_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_success_leaves_only_the_target()  # the same scenario passes unfaulted
    monkeypatch.setattr(status_labels, "plan_transition", _drop_first_remove)
    with expect_violation("P1-SUCCESS-TARGET-ONLY"):
        scenario_success_leaves_only_the_target()


def test_control_remove_before_add_is_detected_mid_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The final labels are correct; only the intermediate boundary is empty.
    fake = ObservingForge({_Q, _FS})
    fake.enforce = False
    _remove_before_add(_forge(fake), ISSUE, _P, (_Q,))
    assert lifecycle_labels(fake.labels) == {_P}

    scenario_single_lifecycle_label_is_never_dropped()
    monkeypatch.setattr(status_labels, "transition_status_label", _remove_before_add)
    with expect_violation("P1-LIFECYCLE-NONEMPTY"):
        scenario_single_lifecycle_label_is_never_dropped()


def test_control_retry_noop_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    position = next(p for p in _POSITIONS if p.name == "add-response-lost")
    scenario_one_retry_converges(position)
    real = status_labels.transition_status_label

    def retry_noop(
        forge: Any, issue_number: Any, new_label: str, old: Any, callback: Any = None
    ) -> None:
        if new_label in forge.labels:  # "already applied": the removals are skipped
            return
        real(forge, issue_number, new_label, old, callback)

    monkeypatch.setattr(status_labels, "transition_status_label", retry_noop)
    with expect_violation("P1-RETRY-CONVERGES"):
        scenario_one_retry_converges(position)


def test_failure_in_the_lazy_old_label_source_is_recoverable_by_one_retry() -> None:
    fake = FaultyForge({*_START})

    def exploding() -> Any:
        yield _Q
        raise Injected("iterable")

    with pytest.raises(Injected):
        transition_status_label(_forge(fake), ISSUE, _P, exploding())
    assert fake.labels == {_B, _FS, _P}

    transition_status_label(_forge(fake), ISSUE, _P, _OLD)
    assert fake.labels == {_P, _FS}


def test_incomplete_old_labels_keep_the_unlisted_label() -> None:
    # queued + done held, only queued listed: a complete retry cannot converge,
    # because the removal snapshot is the caller's responsibility (current behaviour).
    fake = FaultyForge({_Q, StatusLabel.DONE})

    transition_status_label(_forge(fake), ISSUE, _P, (_Q,))
    transition_status_label(_forge(fake), ISSUE, _P, (_Q,))

    assert fake.labels == {StatusLabel.DONE, _P}


@pytest.mark.skipif(
    os.environ.get("HYPOTHESIS_PROFILE", "ci") != "ci",
    reason="another Hypothesis profile was selected explicitly",
)
def test_ci_profile_is_applied_to_the_stateful_test_case() -> None:
    applied: Any = TestStatusLabelMachine.settings
    assert settings.get_current_profile_name() == "ci"
    assert applied.max_examples == 100
    assert applied.stateful_step_count == 30
    assert applied.deadline is None
    assert applied.print_blob is True
